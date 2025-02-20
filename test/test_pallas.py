import logging
import sys
import unittest
from absl.testing import parameterized

import torch
from torch import nn as nn

import torch_xla
import torch_xla.core.xla_model as xm
from torch_xla import runtime as xr
from torch_xla._internal import tpu

import numpy as np

if xr.device_type() == 'TPU':
  from torch_xla.experimental.custom_kernel import jax_import_guard
  jax_import_guard()
  import jax
  import jax.numpy as jnp
  from jax.experimental import pallas as pl


def with_jax_high_precision(func):

  def wrapper(*args, **kwargs):
    jax.config.update('jax_default_matmul_precision', "highest")
    try:
      result = func(*args, **kwargs)
    finally:
      jax.config.update('jax_default_matmul_precision', "default")
    return result

  return wrapper


class PallasTest(parameterized.TestCase):

  # This is to create a diagonal mask where only elements within the same segment
  # can attend to each other. Since the mask is to mask out the unrelevant parts,
  # therefore we use != instead of ==.
  def _make_attention_mask_from_segment_ids(self, q_segment_ids,
                                            kv_segment_ids):
    return q_segment_ids.view(q_segment_ids.shape[0], 1,
                              q_segment_ids.shape[1], 1) != kv_segment_ids.view(
                                  kv_segment_ids.shape[0], 1, 1,
                                  kv_segment_ids.shape[1])

  def _attention(self, q, k, v, *, attn_mask=None, ab=None):
    attn_weight = q @ k.transpose(-2, -1)
    if ab is not None:
      attn_weight = attn_weight + ab
    if attn_mask is not None:
      attn_weight = attn_weight.masked_fill(attn_mask.bool(),
                                            torch.finfo(attn_weight.dtype).min)
    attn_weight = nn.functional.softmax(attn_weight, dim=-1)
    attn_output = attn_weight @ v
    return attn_output

  # The following helper functions prefixed with _pagedattention are used for PagedAttention unit tests
  # Reference: https://github.com/google/jax/blob/main/tests/pallas/paged_attention_kernel_test.py
  def _pagedattention_generate_qkv(
      self,
      seq_lens,
      page_size,
      max_seq_len,
      num_kv_heads,
      num_heads,
      head_dim,
      dtype=torch.float32,
      query_len=None,
  ):
    assert max_seq_len % page_size == 0
    pages_per_sequence = max_seq_len // page_size
    batch_size = len(seq_lens)
    total_pages = batch_size * pages_per_sequence
    k_pages = torch.randn(
        num_kv_heads, total_pages, page_size, head_dim, dtype=dtype)
    v_pages = torch.randn(
        num_kv_heads, total_pages, page_size, head_dim, dtype=dtype)
    page_indices = torch.randperm(
        batch_size * pages_per_sequence, dtype=torch.int32)
    page_indices = page_indices.reshape(batch_size, pages_per_sequence)
    if not query_len:
      q = torch.randn(batch_size, num_heads, head_dim, dtype=dtype)
    else:
      q = torch.randn(batch_size, query_len, num_heads, head_dim, dtype=dtype)
    return q, k_pages, v_pages, page_indices

  def _ragged_pagedattention_generate_qkv(
      self,
      seq_lens,
      num_heads,
      head_dim,
      page_size,
      num_pages,
      dtype=torch.float32,
  ):
    num_seqs = len(seq_lens)
    # Make sure the q_len is no longer than the kv_len. For example,
    # seq_lens = [(1, 1328), (5, 18), (506, 463)] is not a valid test case because
    # the 3rd sequence has q_len(506) > kv_len(463).
    for i in range(num_seqs):
      cur_q_len = seq_lens[i][0]
      cur_kv_len = seq_lens[i][1]
      assert cur_q_len <= cur_kv_len, f"cur_q_len must be less than or equal to cur_kv_len. Got {cur_q_len} and {cur_kv_len}"

    query_lens = [seq_len[0] for seq_len in seq_lens]
    num_q_tokens = sum(query_lens)
    kv_lens = torch.tensor([seq_len[1] for seq_len in seq_lens],
                           dtype=torch.int32)
    num_q_heads = num_heads[0]
    num_kv_heads = num_heads[1]
    assert num_q_heads % num_kv_heads == 0, "num_q_heads % num_kv_heads !=0."
    queries = torch.randn((num_q_tokens, num_q_heads, head_dim), dtype=dtype)
    k_pages = torch.randn((num_kv_heads, num_pages, page_size, head_dim),
                          dtype=dtype)
    v_pages = torch.randn((num_kv_heads, num_pages, page_size, head_dim),
                          dtype=dtype)

    # Create a kv_lens: i32[num_tokens]
    kv_lens_with_paddings = [0] * num_q_tokens
    for i in range(num_seqs):
      kv_lens_with_paddings[i] = kv_lens[i]
    kv_lens_ = torch.tensor(kv_lens_with_paddings, dtype=torch.int32)

    # Create a page_indices i32[num_tokens, pages_per_sequence]
    max_kv_len = max([seq_len[1] for seq_len in seq_lens])
    max_num_pages_per_seq = (max_kv_len + page_size - 1) // page_size

    # The reason why we need to pad max_num_pages_per_seq is that
    # page_indices[1]=max_num_pages_per_seq and max_num_pages_per_seq%num_kv_pages_per_compute_block==0
    max_num_pages_per_seq = 2**int(np.ceil(np.log2(max_num_pages_per_seq)))

    # The assert below mimics the reality that each page get a unique index.
    # But for testing, the assert could be omitted.
    # assert max_num_pages_per_seq*num_q_tokens <= num_pages, f"assert failed: max_num_pages_per_seq*num_q_tokens < num_pages. Got {max_num_pages_per_seq*num_q_tokens} and {num_pages}"
    page_indices = torch.randint(
        0, num_pages, (num_q_tokens, max_num_pages_per_seq), dtype=torch.int32)

    # Create a cu_q_lens i32[num_tokens + 1]
    q_lens_with_paddings = [0] * num_q_tokens
    for i in range(num_seqs):
      q_lens_with_paddings[i] = query_lens[i]
    cu_q_lens = torch.cumsum(
        torch.tensor([0] + q_lens_with_paddings, dtype=torch.int32),
        dim=0,
        dtype=torch.int32)
    return queries, k_pages, v_pages, page_indices, cu_q_lens, kv_lens_

  @unittest.skipIf(xr.device_type() != 'TPU', "This test only works on TPU.")
  def test_tpu_custom_call_pallas_add(self):
    # This payload is generated by the following Pallas code:
    # def add_vectors_kernel(x_ref, y_ref, o_ref):
    #   x, y = x_ref[...], y_ref[...]
    #   o_ref[...] = x + y
    payload = "{\"custom_call_config\": {\"body\": \"TUzvUgFNTElSMTguMC4wZ2l0AAErCwEDBQcJAQMLAwUDDQcFDxEJBRMVA2lNDQFLBw8LEw8PDwsPMwsLCwtlCwsLCwsPCw8PEwsTDwsTDwsPDxMLDwUDYQENGwcTDxsPAsICHx0rLQUXAwMnKRURNx1HSRELAQUZHTM1AwsVFxkbHw0hDSMlBRsBAQUdDQlhZmZpbmVfbWFwPChkMCkgLT4gKGQwKT4ABR8FIQUjBSUFJxEDAQUpFS8JHQ8xFwUTAQUrFwUdAR05OwUtFwUlAR0/QQUvFUMJHQ9FFwUVAQUxFREJI3RwdS5tZW1vcnlfc3BhY2U8dm1lbT4AF0sDIQcdAycDIQcBAgIFBwEBAQEBAgQEpwUBEAEHAwEFAxEBEwcDFScHAQEBAQEBBwMDBwMDCwYDAwUFAQcHAwMHAwMLBgMDBQUDCwkGPQMFBQkNBwMLBwMDCwYLAwUFBRENBAsHDwURBQABBgMBBQEAdgcz2wsTGdkNCxMjIR0pJ0MNCwsTDw8PDQkLEWJ1aWx0aW4AZnVuYwB0cHUAYXJpdGgAdmVjdG9yAG1vZHVsZQByZXR1cm4AY29uc3RhbnQAYWRkaQBsb2FkAHN0b3JlAC9ob21lL2p3dGFuL3BhbGxhcy9wYWxsYXNfYWRkLnB5AGFkZF92ZWN0b3JzX2tlcm5lbABkaW1lbnNpb25fc2VtYW50aWNzAGZ1bmN0aW9uX3R5cGUAc2NhbGFyX3ByZWZldGNoAHNjcmF0Y2hfb3BlcmFuZHMAc3ltX25hbWUAbWFpbgB2YWx1ZQAvZ2V0W3RyZWU9UHlUcmVlRGVmKChDdXN0b21Ob2RlKE5ESW5kZXhlclsoUHlUcmVlRGVmKChDdXN0b21Ob2RlKFNsaWNlWygwLCA4KV0sIFtdKSwpKSwgKDgsKSwgKCkpXSwgW10pLCkpXQBhZGRfdmVjdG9ycwA8bW9kdWxlPgAvYWRkAC9zd2FwW3RyZWU9UHlUcmVlRGVmKChDdXN0b21Ob2RlKE5ESW5kZXhlclsoUHlUcmVlRGVmKChDdXN0b21Ob2RlKFNsaWNlWygwLCA4KV0sIFtdKSwpKSwgKDgsKSwgKCkpXSwgW10pLCkpXQA=\", \"needs_layout_passes\": true}}"

    x = torch.arange(8, dtype=torch.int).to("xla")
    y = torch.arange(8, dtype=torch.int).to("xla")
    expected_output = x + y

    output = torch_xla._XLAC._xla_tpu_custom_call([x, y], payload, [x.shape],
                                                  [x.dtype])
    self.assertTrue(torch.allclose(output[0].cpu(), expected_output.cpu()))

  @unittest.skipIf(xr.device_type() != 'TPU', "This test only works on TPU.")
  def test_tpu_custom_call_pallas_add_one(self):
    # This payload is generated by the following Pallas code:
    # def add_vectors_kernel(x_ref, o_ref):
    #   o_ref[...] = x_ref[...] + 1
    payload = "{\"custom_call_config\": {\"body\": \"TUzvUgFNTElSMTguMC4wZ2l0AAEtCwEDBQcJAQMLAwUDDQcFDxEJBxMVFwNlSQ0BRwcPCw8PDxMLDzMLCwsLZQsLCwsPCw8LEw8PCxMPCxMTDwsLBQNhAQ0bDxMHFw8CpgIfFSsxBRkdQwMdRQMRCwEDAw8nBRsdKQMDCxUXGRsfCyELIyUFHQEBBR8NCWFmZmluZV9tYXA8KGQwKSAtPiAoZDApPgAFIQUjBSUFJxEHAQUpHS0vBSsXBRsBFTM5HTU3BS0XBS8BHTs9BS8XBUUBAwMPQREDBQUxBTMjdHB1Lm1lbW9yeV9zcGFjZTx2bWVtPgAXRwMhAx0BAgInAyEDAwUFAQEBAQIEBKEFARABBwMBBQMRARMHAxMnBQEBAQEHAxENAwcLBhEDBQUBBQcDBz8DAw0GBwMFAwkJBgcDBQUHCwcDCQ0DBwsGCQMFBQMPDwQJBw0DDwUAAQYDAQUBAMIHNdsLEyEv2QsTIyEdKQ1DDRULCxMPDw8NCQsRYnVpbHRpbgBmdW5jAHRwdQBhcml0aAB2ZWN0b3IAbW9kdWxlAHJldHVybgBjb25zdGFudABhZGRpAGxvYWQAYnJvYWRjYXN0AHN0b3JlAC9ob21lL2p3dGFuL3BhbGxhcy9wYWxsYXNfYWRkLnB5AHZhbHVlAGRpbWVuc2lvbl9zZW1hbnRpY3MAZnVuY3Rpb25fdHlwZQBzY2FsYXJfcHJlZmV0Y2gAc2NyYXRjaF9vcGVyYW5kcwBzeW1fbmFtZQBtYWluAC9nZXRbdHJlZT1QeVRyZWVEZWYoKEN1c3RvbU5vZGUoTkRJbmRleGVyWyhQeVRyZWVEZWYoKEN1c3RvbU5vZGUoU2xpY2VbKDAsIDgpXSwgW10pLCkpLCAoOCwpLCAoKSldLCBbXSksKSldAGFkZF9vbmVfdmVjdG9yc19rZXJuZWwAYWRkX3ZlY3RvcnNfb25lADxtb2R1bGU+AC9hZGQAL3N3YXBbdHJlZT1QeVRyZWVEZWYoKEN1c3RvbU5vZGUoTkRJbmRleGVyWyhQeVRyZWVEZWYoKEN1c3RvbU5vZGUoU2xpY2VbKDAsIDgpXSwgW10pLCkpLCAoOCwpLCAoKSldLCBbXSksKSldAA==\", \"needs_layout_passes\": true}}"

    x = torch.arange(8, dtype=torch.int).to("xla")
    expected_output = x + 1

    output = torch_xla._XLAC._xla_tpu_custom_call([x], payload, [x.shape],
                                                  [x.dtype])
    self.assertTrue(torch.allclose(output[0].cpu(), expected_output.cpu()))

  @unittest.skipIf(xr.device_type() != 'TPU', "This test only works on TPU.")
  def test_tpu_custom_call_pallas_raise(self):
    # This payload is generated by the following Pallas code:
    # def add_vectors_kernel(x_ref, o_ref):
    #   o_ref[...] = x_ref[...] + 1
    payload = "{\"custom_call_config\": {\"body\": \"TUzvUgFNTElSMTguMC4wZ2l0AAEtCwEDBQcJAQMLAwUDDQcFDxEJBxMVFwNlSQ0BRwcPCw8PDxMLDzMLCwsLZQsLCwsPCw8LEw8PCxMPCxMTDwsLBQNhAQ0bDxMHFw8CpgIfFSsxBRkdQwMdRQMRCwEDAw8nBRsdKQMDCxUXGRsfCyELIyUFHQEBBR8NCWFmZmluZV9tYXA8KGQwKSAtPiAoZDApPgAFIQUjBSUFJxEHAQUpHS0vBSsXBRsBFTM5HTU3BS0XBS8BHTs9BS8XBUUBAwMPQREDBQUxBTMjdHB1Lm1lbW9yeV9zcGFjZTx2bWVtPgAXRwMhAx0BAgInAyEDAwUFAQEBAQIEBKEFARABBwMBBQMRARMHAxMnBQEBAQEHAxENAwcLBhEDBQUBBQcDBz8DAw0GBwMFAwkJBgcDBQUHCwcDCQ0DBwsGCQMFBQMPDwQJBw0DDwUAAQYDAQUBAMIHNdsLEyEv2QsTIyEdKQ1DDRULCxMPDw8NCQsRYnVpbHRpbgBmdW5jAHRwdQBhcml0aAB2ZWN0b3IAbW9kdWxlAHJldHVybgBjb25zdGFudABhZGRpAGxvYWQAYnJvYWRjYXN0AHN0b3JlAC9ob21lL2p3dGFuL3BhbGxhcy9wYWxsYXNfYWRkLnB5AHZhbHVlAGRpbWVuc2lvbl9zZW1hbnRpY3MAZnVuY3Rpb25fdHlwZQBzY2FsYXJfcHJlZmV0Y2gAc2NyYXRjaF9vcGVyYW5kcwBzeW1fbmFtZQBtYWluAC9nZXRbdHJlZT1QeVRyZWVEZWYoKEN1c3RvbU5vZGUoTkRJbmRleGVyWyhQeVRyZWVEZWYoKEN1c3RvbU5vZGUoU2xpY2VbKDAsIDgpXSwgW10pLCkpLCAoOCwpLCAoKSldLCBbXSksKSldAGFkZF9vbmVfdmVjdG9yc19rZXJuZWwAYWRkX3ZlY3RvcnNfb25lADxtb2R1bGU+AC9hZGQAL3N3YXBbdHJlZT1QeVRyZWVEZWYoKEN1c3RvbU5vZGUoTkRJbmRleGVyWyhQeVRyZWVEZWYoKEN1c3RvbU5vZGUoU2xpY2VbKDAsIDgpXSwgW10pLCkpLCAoOCwpLCAoKSldLCBbXSksKSldAA==\", \"needs_layout_passes\": true}}"

    # _xla_tpu_custom_call requires at least one input.
    with self.assertRaises(RuntimeError):
      torch_xla._XLAC._xla_tpu_custom_call([], payload, [(8, 1)], [torch.int32])
      output.cpu()

  @unittest.skipIf(xr.device_type() != 'TPU', "This test only works on TPU.")
  def test_tpu_custom_call_pallas_flash_attention(self):
    # This payload is generated by the following Pallas code:
    # https://github.com/google/jax/blob/b2058d72b7e1693a41303d5411572aabf99b7981/jax/experimental/pallas/ops/tpu/flash_attention.py#L139
    # To be noted, set `jax.config.update('jax_default_matmul_precision', 'highest')`` before generating the payload.
    payload = "{\"custom_call_config\": {\"body\": \"TUzvUgFNTElSMjAuMC4wZ2l0AAEvCwEDBQcJAQMLAxkNDxETFRcZGx0fISMD0gJaAhsB9QcTCwsPExMLDw8TCwsLC5MLCw8TDwsLCwsLDwsLCw8PCwsPCw8PExMXExMTCw9DCxsLxQuTCwsLCxsbCxsLGwsbCxsbGxsPDw8PFw8LFw8PCxcPDwsXDw8LFw8PCxcPCxcPDxcTHwsPDxcjDxMfCw8XGw8PCw8XCw8LBQmNeZFhBwNdCQNZASsXHwsTFx8PCxMTFxMTFxcfCxMXIwsHA0kBGw8HKx8bBxcPIwsbLy8C0g0fAwMPjwUlBScVl50DAw+NHVICVQUpHSORHSPDHSMuAgUrBS0FLwUxIw8JQQEAAAAAAAAAAQAAAAAAAACAAAAAAAAAAAQAAAAAAAAADRkFMxETAAMD7/8RDwEFNQU3BTkFOwU9Hc3PBT8FQQVDHd0/Fd8JBUUFRwED5QVJHelLFesJHQoCTxUOAgkdHgIiAh1CAlUVRgIJAwNZWwVLEQ8JAw9fYRdjZ2lrKW0pGW9xcwVNAQn19fX5DRdhZmZpbmVfbWFwPChkMCwgZDEsIGQyLCBkMykgLT4gKGQwLCBkMSwgZDIsIGQzKT4ABU8jDwlBAwAAAAAAAAACAAAAAAAAAAEAAAAAAAAAAQAAAAAAAAAFUQVTBVUFVwEJdXl9gQMFG3cdHwkrAwUbex0fCS0DBRt/HR8JLwMFG4MdHwkxAwUXIRkrAwUXIRktAwUXIRkvAwUXIRkxEQEBEQMBFZMJHQeVFwUGCAEdmZsFWRcFSgUBFZ+lHaGjBVsXBYYLARWnrR2pqwVdFwViAwEVr7UdsbMFXxcFGgMBFbe9Hbm7BWEXM14DAR2/wQVjFzM2EAEVxQkdB8cXBQoIAQMDD8slBwkAAAAABWUV0QkdB9MXBQ4IAQMHN/c5JTvXERMBAwMP2yUNCQAAgP8FZx0H4RcFoggBAwVB/UNFEQ8FHUc/BWkdB+0XBaYIAQVrHfNLBW0jdHB1LmRpbWVuc2lvbl9zZW1hbnRpY3M8cGFyYWxsZWw+ACN0cHUuY29udHJhY3RfcHJlY2lzaW9uPGZwMzI+ACN0cHUuZGltZW5zaW9uX3NlbWFudGljczxhcmJpdHJhcnk+ACN0cHUubWVtb3J5X3NwYWNlPHZtZW0+ACN2ZWN0b3Iua2luZDxtYXhpbXVtZj4AI2FyaXRoLmZhc3RtYXRoPG5vbmU+AAMDDwYCJQ0JAAAAAAVvHQcSAhcFqggBAwVBVgJDRR1HTwVxFSYCCR0HKgIXBa4IARUyAgkdBzYCFwXKCAEDAw8+AiUJCQAAAAAFcx0HSgIXBc4IAQMHN/c5JTslBXUjdmVjdG9yLmtpbmQ8YWRkPgABAgIDF/sJBQUCBBELZScFAgQCBAsnBQIEEQsLJwMCBAsBAgQnCQUFAgQRCwEJJwUCBAULBREBAQEBBQUFBQEFCQEBAQEJAQEBAQSuBwUBEQFXBwMBFQcRAV0HA2GrEQEBAQEBAQEBBQEFAQUBBQEDAxEDAwMDAxEDAwMDAxEDAwMDAxEDAwMLBhEDEQsJERMVFwUGEQMJAxkDAxMDAwMDAxMDAwMDAxMDAwMDAxMDAwMLBhMDEQsLHR8hIwUGEwMJAyUDAzXJAwcNBzXVAwcHGycpAwM92QMNDwc94wMNBSstBQbnAxUDLxEGSQMHAzETB0knAwcFKzMVB/EnAwcDNQMDTQICAw0PB00WAgMNBTc5BQYaAgMVAzsRBlEDBwM9FwdRJwMHBTc/AwMVAwMDAwMVAwMDAwMVAwMDAwMVAwMDCwYVAxELDUNFR0kFBhUDCQNLAwNTOgIDCQ0HU04CAwkHQU1PAwMNAwMDAwMNAwMDAwMNAwMDAwMNAwMDCwYNAxELD1NVV1kFBg0DCQNbBQYNAxEDURkEDQ1fD1NVV1kJAAEHEQGFBwMNDwkBAQEBAQEBAQMDAQsDAQMDAQsDAQkEAQkBAwUJBxEBhwcDDQ8JAQEBAQEBAQEDAwELAwEDAwELAwEJBAEJAQMHCQcRAYkHAw0PCQEBAQEBAQEBAwMBCwMBAwMBCwMBCQQBCQEDBwkHEQGLBwMNDwkBAQEBAQEBAQMDAQsDAQMDAQsDAQkEAQkBAwUJBgMBBQEAMhp37gImAgsvCxMLLyYCE2MhIy0xHQsjISMpLXkfCx0dFVkZGRkZ6gIdJRMdDWPvGxcTFyMvFxkZFSUfDw0PCR0RYnVpbHRpbgBzdGFibGVfbW9zYWljAHRwdQB2ZWN0b3IAYXJpdGgAbW9kdWxlAGFyaXRoLmNvbnN0YW50AHZlY3Rvci5zaGFwZV9jYXN0AGZ1bmMuZnVuYwBmdW5jLnJldHVybgB2ZWN0b3IubG9hZAB0cHUubWF0bXVsAHZlY3Rvci5tdWx0aV9yZWR1Y3Rpb24AdmVjdG9yLmJyb2FkY2FzdABhcml0aC5zdWJmAG1hdGguZXhwAGFyaXRoLmRpdmYAdmVjdG9yLnN0b3JlAC9ob21lL2JiYWhsL21pbmljb25kYTMvZW52cy90b3JjaHNlcDEwL2xpYi9weXRob24zLjEwL3NpdGUtcGFja2FnZXMvamF4L2V4cGVyaW1lbnRhbC9wYWxsYXMvb3BzL3RwdS9mbGFzaF9hdHRlbnRpb24ucHkAX2ZsYXNoX2F0dGVudGlvbl9rZXJuZWxfc2luZ2xlX2JhdGNoX3NpbmdsZV9zdGVwAHZhbHVlAGZ1bmN0aW9uX3R5cGUAc3ltX25hbWUAdHJhbnNmb3JtX2luZGljZXMAd2luZG93X2JvdW5kcwAvZ2V0W3RyZWU9UHlUcmVlRGVmKChDdXN0b21Ob2RlKE5ESW5kZXhlclsoUHlUcmVlRGVmKCgqLCAqLCBDdXN0b21Ob2RlKFNsaWNlWygwLCAxMjgsIDEpXSwgW05vbmUsIE5vbmVdKSwgQ3VzdG9tTm9kZShTbGljZVsoMCwgNCwgMSldLCBbTm9uZSwgTm9uZV0pKSksICgxLCAxLCAxMjgsIDQpLCAoKSldLCBbKiwgKl0pLCkpXQB0cmFuc2Zvcm1fMAB0cmFuc2Zvcm1fMQB0cmFuc2Zvcm1fMgB0cmFuc2Zvcm1fMwAvaG9tZS9iYmFobC9weXRvcmNoL3hsYS90ZXN0L3Rlc3RfcGFsbGFzLnB5AHByZWNpc2lvbgB0cmFuc3Bvc2VfbGhzAHRyYW5zcG9zZV9yaHMAa2luZAByZWR1Y3Rpb25fZGltcwAvYnJvYWRjYXN0X2luX2RpbVtzaGFwZT0oMTI4LCAxKSBicm9hZGNhc3RfZGltZW5zaW9ucz0oMCwpXQBzdGFibGVfbW9zYWljLnZlcnNpb24AZGltZW5zaW9uX3NlbWFudGljcwBpdGVyYXRpb25fYm91bmRzAHNjYWxhcl9wcmVmZXRjaABzY3JhdGNoX29wZXJhbmRzAG1haW4Ad2luZG93X3BhcmFtcwBfZmxhc2hfYXR0ZW50aW9uX2tlcm5lbABfZmxhc2hfYXR0ZW50aW9uX2ltcGwAX2ZsYXNoX2F0dGVudGlvbgBmbGFzaF9hdHRlbnRpb24AdGVzdF90cHVfY3VzdG9tX2NhbGxfcGFsbGFzX3dyYXBfZmxhc2hfYXR0ZW50aW9uADxtb2R1bGU+AC9kb3RfZ2VuZXJhbFtkaW1lbnNpb25fbnVtYmVycz0oKCgxLCksICgxLCkpLCAoKCksICgpKSkgcHJlY2lzaW9uPShQcmVjaXNpb24uSElHSEVTVCwgUHJlY2lzaW9uLkhJR0hFU1QpIHByZWZlcnJlZF9lbGVtZW50X3R5cGU9ZmxvYXQzMl0AL3JlZHVjZV9tYXhbYXhlcz0oMSwpXQAvc3ViAGZhc3RtYXRoAC9leHAAL3JlZHVjZV9zdW1bYXhlcz0oMSwpXQAvZGl2AC9kb3RfZ2VuZXJhbFtkaW1lbnNpb25fbnVtYmVycz0oKCgxLCksICgwLCkpLCAoKCksICgpKSkgcHJlY2lzaW9uPShQcmVjaXNpb24uSElHSEVTVCwgUHJlY2lzaW9uLkhJR0hFU1QpIHByZWZlcnJlZF9lbGVtZW50X3R5cGU9ZmxvYXQzMl0AL3N3YXBbdHJlZT1QeVRyZWVEZWYoKEN1c3RvbU5vZGUoTkRJbmRleGVyWyhQeVRyZWVEZWYoKCosICosIEN1c3RvbU5vZGUoU2xpY2VbKDAsIDEyOCwgMSldLCBbTm9uZSwgTm9uZV0pLCBDdXN0b21Ob2RlKFNsaWNlWygwLCA0LCAxKV0sIFtOb25lLCBOb25lXSkpKSwgKDEsIDEsIDEyOCwgNCksICgpKV0sIFsqLCAqXSksKSldAA==\", \"serialization_format\": 1, \"needs_layout_passes\": true}, \"implicit_sharding\": {\"type\": \"MANUAL\"}}"
    # The division is to cause potential precision issue on TPU.
    q_mini = torch.arange(128 * 4, dtype=torch.float32).reshape(128, 4) / 13
    k_mini = torch.arange(
        1000, 1000 + 128 * 4, dtype=torch.float32).reshape(128, 4) / 13
    q = q_mini.broadcast_to(3, 2, 128, 4).to("xla")
    k = k_mini.broadcast_to(3, 2, 128, 4).to("xla")
    v = torch.ones(3, 2, 128, 4).to("xla")

    expected_o = self._attention(q, k, v)

    o = torch_xla._XLAC._xla_tpu_custom_call([q, k, v], payload, [q.shape],
                                             [q.dtype])
    self.assertTrue(torch.allclose(o[0].cpu(), expected_o.cpu()))

  @unittest.skipIf(xr.device_type() != 'TPU', "This test only works on TPU.")
  def test_tpu_custom_call_pallas_extract_add_payload(self):
    import jax._src.pallas.mosaic.pallas_call_registration

    def add_vectors_kernel(x_ref, y_ref, o_ref):
      x, y = x_ref[...], y_ref[...]
      o_ref[...] = x + y

    @jax.jit
    def add_vectors(x: jax.Array, y: jax.Array) -> jax.Array:
      return pl.pallas_call(
          add_vectors_kernel, out_shape=jax.ShapeDtypeStruct(x.shape,
                                                             x.dtype))(x, y)

    import torch_xla.experimental.custom_kernel as custom_kernel

    ir = jax.jit(add_vectors).lower(jnp.arange(8), jnp.arange(8)).compiler_ir()
    payload = custom_kernel._extract_backend_config(ir)
    # The payload being generated could vary each time. We just want to make sure
    # the most important fields are present.
    self.assertIn("custom_call_config", payload)

  @unittest.skipIf(xr.device_type() != 'TPU', "This test only works on TPU.")
  def test_tpu_custom_call_pallas_wrap_add_payload(self):

    def add_vectors_kernel(x_ref, y_ref, o_ref):
      x, y = x_ref[...], y_ref[...]
      o_ref[...] = x + y

    @jax.jit
    def add_vectors(x: jax.Array, y: jax.Array) -> jax.Array:
      return pl.pallas_call(
          add_vectors_kernel, out_shape=jax.ShapeDtypeStruct(x.shape,
                                                             x.dtype))(x, y)

    from torch_xla.experimental.custom_kernel import make_kernel_from_pallas
    pt_kernel = make_kernel_from_pallas(add_vectors,
                                        lambda x, y: [(x.shape, x.dtype)])

    dtypes = [
        torch.float32, torch.float
    ]  # Add doesn't support torch.float64, torch.bfloat16, torch.float16.
    for i in range(len(dtypes)):
      x = torch.randn((i + 1, i + 1), dtype=dtypes[i]).to("xla")
      y = torch.randn((i + 1, i + 1), dtype=dtypes[i]).to("xla")
      expected_output = x + y
      output = pt_kernel(x, y)
      self.assertTrue(torch.allclose(output.cpu(), expected_output.cpu()))

    dtypes = [
        torch.int32, torch.int
    ]  # Add doesn't support torch.int64, torch.int16, torch.int8, torch.uint8.
    for i in range(len(dtypes)):
      x = torch.arange(i + 1, dtype=dtypes[i]).to("xla")
      y = torch.arange(i + 1, dtype=dtypes[i]).to("xla")
      expected_output = x + y
      output = pt_kernel(x, y)
      self.assertTrue(torch.allclose(output.cpu(), expected_output.cpu()))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  def test_tpu_custom_call_pallas_wrap_flash_attention(self):
    from jax.experimental.pallas.ops.tpu.flash_attention import flash_attention
    from torch_xla.experimental.custom_kernel import make_kernel_from_pallas
    flash_attention_kernel = make_kernel_from_pallas(
        flash_attention, lambda q, k, v: [(q.shape, q.dtype)])

    q_mini = torch.arange(128 * 4, dtype=torch.bfloat16).reshape(128, 4) / 13
    k_mini = torch.arange(
        1000, 1000 + 128 * 4, dtype=torch.bfloat16).reshape(128, 4) / 13
    q = q_mini.broadcast_to(3, 2, 128, 4).to("xla")
    k = k_mini.broadcast_to(3, 2, 128, 4).to("xla")
    v = torch.ones(3, 2, 128, 4, dtype=torch.bfloat16).to("xla")

    o = flash_attention_kernel(q, k, v)
    expected_o = self._attention(q, k, v)
    torch.testing.assert_close(o.cpu(), expected_o.cpu())
    # self.assertTrue(torch.allclose(o.cpu(), expected_o.cpu()))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_wrapper(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")

    o = flash_attention(q, k, v)
    expected_o = self._attention(q, k, v)
    self.assertTrue(torch.allclose(o.cpu(), expected_o.cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_wrapper_kv_and_ab_padding(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    q = torch.randn(1, 2, 513, 4).to("xla")
    k = torch.randn(1, 2, 513, 4).to("xla")
    v = torch.randn(1, 2, 513, 4).to("xla")
    ab = torch.randn(1, 2, 513, 513).to("xla")

    o = flash_attention(q, k, v, ab=ab)
    expected_o = self._attention(q, k, v, ab=ab)
    self.assertTrue(torch.allclose(o.cpu(), expected_o.cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_wrapper_with_dynamo(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    def flash_attention_wrapper(q, k, v, causal=False):
      return torch.ops.xla.flash_attention(q, k, v, causal)

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")

    compiled_flash_attention = torch.compile(
        flash_attention_wrapper, backend="openxla")
    o_no_causal = compiled_flash_attention(q, k, v)
    o_with_causal = compiled_flash_attention(q, k, v, causal=True)
    expected_o = self._attention(q, k, v)
    self.assertTrue(torch.allclose(o_no_causal.cpu(), expected_o.cpu()))
    # The causal mask is turned on by default in the wrapper.
    # It masks out the top right triangle of the attention matrix,
    # therefore it speeds up the compute but also changes the output.
    self.assertFalse(
        torch.allclose(o_with_causal.cpu(), expected_o.cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_wrapper_causal(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")

    # The causal mask is turned on by default in the wrapper.
    # It masks out the top right triangle of the attention matrix, therefore it speeds up the compute but also changes the output.
    o = flash_attention(q, k, v, causal=True)
    expected_o = self._attention(q, k, v)
    self.assertFalse(torch.allclose(o.cpu(), expected_o.cpu()))

  @unittest.skipIf(xr.device_type() != 'TPU', "This test only works on TPU.")
  def test_multiple_returns(self):
    import jax._src.pallas.mosaic.pallas_call_registration

    def add_minus_vectors_kernel(x_ref, y_ref, o1_ref, o2_ref):
      x, y = x_ref[...], y_ref[...]
      o1_ref[...] = x + y
      o2_ref[...] = x - y

    @jax.jit
    def add_minus_vectors(x: jax.Array, y: jax.Array) -> jax.Array:
      out_shape = jax.ShapeDtypeStruct(x.shape, x.dtype)
      return pl.pallas_call(
          add_minus_vectors_kernel, out_shape=[out_shape, out_shape])(x, y)

    from torch_xla.experimental.custom_kernel import make_kernel_from_pallas
    pt_kernel = make_kernel_from_pallas(
        add_minus_vectors, lambda x, y: [(x.shape, x.dtype),
                                         (x.shape, x.dtype)])
    x = torch.arange(8, device="xla", dtype=torch.float)
    o = pt_kernel(x, x)
    self.assertEqual(len(o), 2)

    expected_o0 = x + x
    expected_o1 = x - x
    self.assertTrue(torch.allclose(o[0].cpu(), expected_o0.cpu()))
    self.assertTrue(torch.allclose(o[1].cpu(), expected_o1.cpu()))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  def test__flash_attention_impl(self):
    from jax.experimental.pallas.ops.tpu.flash_attention import _flash_attention_impl
    from torch_xla.experimental.custom_kernel import make_kernel_from_pallas
    MIN_BLOCK_SIZE = 128

    def shape_dtype(q, *arg):
      res_shape = list(q.shape)
      res_shape[-1] = MIN_BLOCK_SIZE
      return [(q.shape, q.dtype), (res_shape, torch.float32),
              (res_shape, torch.float32)]

    flash_attention_kernel = make_kernel_from_pallas(_flash_attention_impl,
                                                     shape_dtype)

    q = torch.randn(3, 2, 128, 4, dtype=torch.bfloat16).to("xla")
    k = torch.randn(3, 2, 128, 4, dtype=torch.bfloat16).to("xla")
    v = torch.randn(3, 2, 128, 4, dtype=torch.bfloat16).to("xla")

    o, l, m = flash_attention_kernel(
        q,
        k,
        v,
        None,
        None,
        True,
        False,
        1.0,
        2,
        128,
        128,
        128,
        False,
        static_argnums=range(5, 13))
    xm.mark_step()

    # TODO: I don't really know how to test the value. Let's do the shape check for now.
    self.assertEqual(l.shape, (3, 2, 128, MIN_BLOCK_SIZE))
    self.assertEqual(l.dtype, torch.float32)
    self.assertEqual(m.shape, (3, 2, 128, MIN_BLOCK_SIZE))
    self.assertEqual(m.dtype, torch.float32)

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  def test__flash_attention_bwd_dkv(self):
    from jax.experimental.pallas.ops.tpu.flash_attention import _flash_attention_bwd_dkv
    from torch_xla.experimental.custom_kernel import trace_pallas
    MIN_BLOCK_SIZE = 128
    DEFAULT_MASK_VALUE = -0.7 * float(torch.finfo(torch.float32).max)

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")
    l = torch.randn(3, 2, 128).to("xla")
    m = torch.randn(3, 2, 128).to("xla")
    grad_i = torch.randn(3, 2, 128, dtype=torch.float32).to("xla")
    grad_o = torch.randn(3, 2, 128, 4).to("xla")

    payload, _ = trace_pallas(
        _flash_attention_bwd_dkv,
        q,
        k,
        v,
        None,
        None,
        l,
        m,
        grad_o,
        grad_i,
        block_q_major=128,
        block_k_major=128,
        block_k=128,
        block_q=128,
        sm_scale=1.0,
        causal=False,
        mask_value=DEFAULT_MASK_VALUE,
        debug=False,
        static_argnames=[
            "block_q_major", "block_k_major", "block_k", "block_q", "sm_scale",
            "causal", "mask_value", "debug"
        ])

    # TODO: Because of the following reshapes, we can't use make_kernel_from_pallas directly.
    l = l.unsqueeze(-1).expand(3, 2, 128, MIN_BLOCK_SIZE)
    m = m.unsqueeze(-1).expand(3, 2, 128, MIN_BLOCK_SIZE)
    grad_i = grad_i.unsqueeze(-1).expand(3, 2, 128, MIN_BLOCK_SIZE)
    output = torch_xla._XLAC._xla_tpu_custom_call(
        [q, k, v, l, m, grad_o, grad_i], payload, [k.shape, v.shape],
        [k.dtype, v.dtype])

    xm.mark_step()

    # TODO: I don't really know how to test the value. Let's do the shape check for now.
    self.assertEqual(output[0].shape, (3, 2, 128, 4))
    self.assertEqual(output[1].shape, (3, 2, 128, 4))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  def test__flash_attention_bwd_dkv(self):
    from jax.experimental.pallas.ops.tpu.flash_attention import _flash_attention_bwd_dq
    from torch_xla.experimental.custom_kernel import trace_pallas
    MIN_BLOCK_SIZE = 128
    DEFAULT_MASK_VALUE = -0.7 * float(torch.finfo(torch.float32).max)

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")
    l = torch.randn(3, 2, 128).to("xla")
    m = torch.randn(3, 2, 128).to("xla")
    grad_i = torch.randn(3, 2, 128, dtype=torch.float32).to("xla")
    grad_o = torch.randn(3, 2, 128, 4).to("xla")

    payload, _ = trace_pallas(
        _flash_attention_bwd_dq,
        q,
        k,
        v,
        None,
        None,
        l,
        m,
        grad_o,
        grad_i,
        block_q_major=128,
        block_k_major=128,
        block_k=128,
        sm_scale=1.0,
        causal=False,
        mask_value=DEFAULT_MASK_VALUE,
        debug=False,
        static_argnames=[
            "block_q_major", "block_k_major", "block_k", "sm_scale", "causal",
            "mask_value", "debug"
        ])

    # TODO: Because of the following reshapes, we can't use make_kernel_from_pallas directly.
    l = l.unsqueeze(-1).expand(3, 2, 128, MIN_BLOCK_SIZE)
    m = m.unsqueeze(-1).expand(3, 2, 128, MIN_BLOCK_SIZE)
    grad_i = grad_i.unsqueeze(-1).expand(3, 2, 128, MIN_BLOCK_SIZE)
    output = torch_xla._XLAC._xla_tpu_custom_call(
        [q, k, v, l, m, grad_o, grad_i], payload, [q.shape], [q.dtype])

    xm.mark_step()

    # TODO: I don't really know how to test the value. Let's do the shape check for now.
    self.assertEqual(output[0].shape, (3, 2, 128, 4))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_backward(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()

    o = flash_attention(q, k, v)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    q_grad = q.grad
    k_grad = k.grad
    v_grad = v.grad

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()

    o = self._attention(q, k, v)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    for i in [(q, q_grad), (k, k_grad), (v, v_grad)]:
      self.assertTrue(torch.allclose(i[0].grad.cpu(), i[1].cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  def test_paged_attention_wrapper(self):
    from torch_xla.experimental.custom_kernel import paged_attention
    from jax.experimental.pallas.ops.tpu.paged_attention.paged_attention_kernel import paged_attention as jax_paged_attention

    max_kv_len = 2048
    block_size = 512
    page_size = 64
    num_kv_heads = 8
    q_kv_head_ratio = 8
    head_dim = 256
    dtype = torch.float32
    seq_lens = torch.tensor([0, 3, 256, 513, 1023, 2048], dtype=torch.int32)

    q, k_pages, v_pages, page_indices = self._pagedattention_generate_qkv(
        seq_lens,
        page_size,
        max_kv_len,
        num_kv_heads,
        num_kv_heads * q_kv_head_ratio,
        head_dim,
    )

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    seq_lens_xla = seq_lens.to("xla")
    page_indices_xla = page_indices.to("xla")

    output = paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        seq_lens_xla,
        page_indices_xla,
        pages_per_compute_block=block_size // page_size,
    )

    q_jax = jnp.array(q.numpy(), dtype=jnp.float32)
    k_pages_jax = jnp.array(k_pages.numpy(), dtype=jnp.float32)
    v_pages_jax = jnp.array(v_pages.numpy(), dtype=jnp.float32)
    seq_lens_jax = jnp.array(seq_lens.numpy(), dtype=jnp.int32)
    page_indices_jax = jnp.array(page_indices.numpy(), dtype=jnp.int32)
    expected_output = torch.from_numpy(
        np.array(
            jax_paged_attention(
                q_jax,
                k_pages_jax,
                v_pages_jax,
                seq_lens_jax,
                page_indices_jax,
                pages_per_compute_block=block_size // page_size,
            )))

    self.assertTrue(
        torch.allclose(
            output.cpu()[seq_lens > 0],
            expected_output.cpu()[seq_lens > 0],
            atol=1e-5,
            rtol=1e-5))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  def test_ragged_paged_attention_wrapper_without_dynamo(self):
    from torch_xla.experimental.custom_kernel import ragged_paged_attention
    from torch_xla.experimental.pallas_kernels.ragged_paged_attention_kernel import ragged_paged_attention as jax_ragged_paged_attention

    seq_lens = [
        (1, 1328),
        (5, 18),
        (1, 129),
        (120, 229),
        (1, 122),  # first physical q block
        (1, 64),
        (32, 100),
        (250, 463),
        (1, 18),
        (1, 17),
        (99, 123)
    ]  # last 3 physical q blocks [(q_len, kv_len),...]
    num_heads = (4, 4)
    head_dim = 128
    dtype = torch.float32
    page_size = 16
    num_pages = 32768
    num_seqs = len(seq_lens)
    num_kv_pages_per_block = 128
    num_queries_per_block = 8
    block_kv_size = 256

    q, k_pages, v_pages, page_indices, cu_q_lens, kv_lens = self._ragged_pagedattention_generate_qkv(
        seq_lens, num_heads, head_dim, page_size, num_pages, dtype=dtype)

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    kv_lens_xla = kv_lens.to("xla")
    page_indices_xla = page_indices.to("xla")
    cu_q_lens_xla = cu_q_lens.to("xla")

    output = ragged_paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        kv_lens_xla,
        page_indices_xla,
        cu_q_lens_xla,
        num_seqs=num_seqs,
        num_kv_pages_per_block=num_kv_pages_per_block,
        num_queries_per_block=num_queries_per_block,
        use_kernel=True)

    nonkernel_output = ragged_paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        kv_lens_xla,
        page_indices_xla,
        cu_q_lens_xla,
        num_seqs=num_seqs,
        num_kv_pages_per_block=num_kv_pages_per_block,
        num_queries_per_block=num_queries_per_block,
        use_kernel=False)

    q_jax = jnp.array(q.numpy(), dtype=jnp.float32)
    k_pages_jax = jnp.array(k_pages.numpy(), dtype=jnp.float32)
    v_pages_jax = jnp.array(v_pages.numpy(), dtype=jnp.float32)
    kv_lens_jax = jnp.array(kv_lens.numpy(), dtype=jnp.int32)
    page_indices_jax = jnp.array(page_indices.numpy(), dtype=jnp.int32)
    cu_q_lens_jax = jnp.array(cu_q_lens.numpy(), dtype=jnp.int32)

    expected_output = torch.from_numpy(
        np.array(
            jax_ragged_paged_attention(
                q_jax,
                k_pages_jax,
                v_pages_jax,
                kv_lens_jax,
                page_indices_jax,
                cu_q_lens_jax,
                num_seqs=num_seqs,
                num_kv_pages_per_block=num_kv_pages_per_block,
                num_queries_per_block=num_queries_per_block,
            )[1]))

    self.assertTrue(
        torch.allclose(
            output.cpu(), expected_output.cpu(), atol=1e-5, rtol=1e-5))
    self.assertTrue(
        torch.allclose(
            output.cpu(), nonkernel_output.cpu(), atol=2e-1, rtol=1e-2))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  def test_ragged_paged_attention_wrapper_with_dynamo(self):
    seq_lens = [
        (1, 1328),
        (5, 18),
        (1, 129),
        (120, 229),
        (1, 122),  # first physical q block
        (1, 64),
        (32, 100),
        (250, 463),
        (1, 18),
        (1, 17),
        (99, 123)
    ]  # last 3 physical q blocks [(q_len, kv_len),...]
    num_heads = (4, 4)
    head_dim = 128
    dtype = torch.float32
    page_size = 16
    num_pages = 32768
    num_seqs = len(seq_lens)
    num_kv_pages_per_block = 128
    num_queries_per_block = 8
    block_kv_size = 256

    q, k_pages, v_pages, page_indices, cu_q_lens, kv_lens = self._ragged_pagedattention_generate_qkv(
        seq_lens, num_heads, head_dim, page_size, num_pages, dtype=dtype)

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    kv_lens_xla = kv_lens.to("xla")
    page_indices_xla = page_indices.to("xla")
    cu_q_lens_xla = cu_q_lens.to("xla")

    def ragged_paged_attention_wrapper(q, k_pages, v_pages, kv_lens,
                                       page_indices, cu_q_lens, num_seqs,
                                       num_kv_pages_per_block,
                                       num_queries_per_block, use_kernel):
      return torch.ops.xla.ragged_paged_attention(
          q,
          k_pages,
          v_pages,
          kv_lens,
          page_indices,
          cu_q_lens,
          num_seqs,
          num_kv_pages_per_block,
          num_queries_per_block,
          use_kernel=use_kernel,
      )

    compiled_paged_attention = torch.compile(
        ragged_paged_attention_wrapper, backend="openxla")

    output = compiled_paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        kv_lens_xla,
        page_indices_xla,
        cu_q_lens_xla,
        num_seqs=num_seqs,
        num_kv_pages_per_block=num_kv_pages_per_block,
        num_queries_per_block=num_queries_per_block,
        use_kernel=True,
    )

    nonkernel_output = compiled_paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        kv_lens_xla,
        page_indices_xla,
        cu_q_lens_xla,
        num_seqs=num_seqs,
        num_kv_pages_per_block=num_kv_pages_per_block,
        num_queries_per_block=num_queries_per_block,
        use_kernel=False,
    )

    self.assertTrue(
        torch.allclose(
            output.cpu(), nonkernel_output.cpu(), atol=2e-1, rtol=1e-2))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  def test_paged_attention_multi_queries_wrapper(self):
    from torch_xla.experimental.custom_kernel import multi_queries_paged_attention
    from torch_xla.experimental.pallas_kernels.multi_queries_paged_attention_kernel import paged_attention as jax_multi_queries_paged_attention

    dtype = torch.float32
    page_size = 16
    num_kv_heads = 8
    q_kv_head_ratio = 4
    head_dim = 256
    num_queries_per_compute_block = 32
    block_kv_size = 256

    max_kv_len = 2048
    query_len = 64
    batch_size = 3
    kv_seq_lens = torch.randint(
        query_len, max_kv_len, (batch_size,), dtype=torch.int32)
    effective_q_lens = torch.full((batch_size,), query_len, dtype=torch.int32)
    assert query_len <= max_kv_len
    for cur_kv_seq in kv_seq_lens:
      assert query_len <= cur_kv_seq, f'{query_len} should be less than or equal to the kv_len {cur_kv_seq} in the current sequence.'
    batch_size = len(kv_seq_lens)
    pages_per_sequence = max_kv_len // page_size
    total_num_pages = batch_size * pages_per_sequence
    assert max_kv_len <= total_num_pages * page_size

    q, k_pages, v_pages, page_indices = self._pagedattention_generate_qkv(
        kv_seq_lens,
        page_size,
        max_kv_len,
        num_kv_heads,
        num_kv_heads * q_kv_head_ratio,
        head_dim,
        dtype=dtype,
        query_len=query_len,
    )

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    kv_seq_lens_xla = kv_seq_lens.to("xla")
    page_indices_xla = page_indices.to("xla")
    effective_q_lens_xla = effective_q_lens.to("xla")

    output_no_cap = multi_queries_paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        kv_seq_lens_xla,
        page_indices_xla,
        effective_q_lens_xla,
        num_kv_pages_per_compute_block=block_kv_size // page_size,
        num_queries_per_compute_block=num_queries_per_compute_block,
    )

    output = multi_queries_paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        kv_seq_lens_xla,
        page_indices_xla,
        effective_q_lens_xla,
        num_kv_pages_per_compute_block=block_kv_size // page_size,
        num_queries_per_compute_block=num_queries_per_compute_block,
        attn_logits_soft_cap=1.0,
    )

    nonkernel_output = multi_queries_paged_attention(
        q_xla,
        k_pages_xla,
        v_pages_xla,
        kv_seq_lens_xla,
        page_indices_xla,
        effective_q_lens_xla,
        num_kv_pages_per_compute_block=block_kv_size // page_size,
        num_queries_per_compute_block=num_queries_per_compute_block,
        use_kernel=False,
    )

    q_jax = jnp.array(q.numpy(), dtype=jnp.float32)
    k_pages_jax = jnp.array(k_pages.numpy(), dtype=jnp.float32)
    v_pages_jax = jnp.array(v_pages.numpy(), dtype=jnp.float32)
    kv_seq_lens_jax = jnp.array(kv_seq_lens.numpy(), dtype=jnp.int32)
    page_indices_jax = jnp.array(page_indices.numpy(), dtype=jnp.int32)
    effective_q_lens_jax = jnp.array(effective_q_lens.numpy(), dtype=jnp.int32)
    expected_output = torch.from_numpy(
        np.array(
            jax_multi_queries_paged_attention(
                q_jax,
                k_pages_jax,
                v_pages_jax,
                kv_seq_lens_jax,
                page_indices_jax,
                effective_q_lens_jax,
                num_kv_pages_per_compute_block=block_kv_size // page_size,
                num_queries_per_compute_block=num_queries_per_compute_block,
                attn_logits_soft_cap=1.0,
            )))
    expected_output_no_cap = torch.from_numpy(
        np.array(
            jax_multi_queries_paged_attention(
                q_jax,
                k_pages_jax,
                v_pages_jax,
                kv_seq_lens_jax,
                page_indices_jax,
                effective_q_lens_jax,
                num_kv_pages_per_compute_block=block_kv_size // page_size,
                num_queries_per_compute_block=num_queries_per_compute_block,
            )))

    self.assertTrue(
        torch.allclose(
            output.cpu(), expected_output.cpu(), atol=1e-5, rtol=1e-5))
    self.assertFalse(
        torch.allclose(
            output.cpu(), expected_output_no_cap.cpu(), atol=1e-5, rtol=1e-5))
    self.assertTrue(
        torch.allclose(
            output_no_cap.cpu(),
            expected_output_no_cap.cpu(),
            atol=1e-5,
            rtol=1e-5))
    self.assertTrue(
        torch.allclose(
            output_no_cap.cpu(), nonkernel_output.cpu(), atol=1e-2, rtol=1e-2))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  def test_paged_attention_multi_queries_wrapper_with_dynamo(self):
    dtype = torch.float32
    page_size = 16
    num_kv_heads = 8
    q_kv_head_ratio = 4
    head_dim = 256
    num_queries_per_compute_block = 32
    block_kv_size = 256

    max_kv_len = 2048
    query_len = 64
    batch_size = 3
    kv_seq_lens = torch.randint(
        query_len, max_kv_len, (batch_size,), dtype=torch.int32)
    effective_q_lens = torch.full((batch_size,), query_len, dtype=torch.int32)
    assert query_len <= max_kv_len
    for cur_kv_seq in kv_seq_lens:
      assert query_len <= cur_kv_seq, f'{query_len} should be less than or equal to the kv_len {cur_kv_seq} in the current sequence.'
    batch_size = len(kv_seq_lens)
    pages_per_sequence = max_kv_len // page_size
    total_num_pages = batch_size * pages_per_sequence
    assert max_kv_len <= total_num_pages * page_size

    q, k_pages, v_pages, page_indices = self._pagedattention_generate_qkv(
        kv_seq_lens,
        page_size,
        max_kv_len,
        num_kv_heads,
        num_kv_heads * q_kv_head_ratio,
        head_dim,
        dtype=dtype,
        query_len=query_len,
    )

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    kv_seq_lens_xla = kv_seq_lens.to("xla")
    page_indices_xla = page_indices.to("xla")
    effective_q_lens_xla = effective_q_lens.to("xla")

    def multi_queries_paged_attention_wrapper(q, k_pages, v_pages, kv_seq_lens,
                                              page_indices, effective_q_lens,
                                              num_kv_pages_per_compute_block,
                                              num_queries_per_compute_block,
                                              use_kernel, attn_logits_soft_cap):
      return torch.ops.xla.multi_queries_paged_attention(
          q,
          k_pages,
          v_pages,
          kv_seq_lens,
          page_indices,
          effective_q_lens,
          num_kv_pages_per_compute_block,
          num_queries_per_compute_block,
          use_kernel=use_kernel,
          attn_logits_soft_cap=attn_logits_soft_cap,
      )

    compiled_paged_attention = torch.compile(
        multi_queries_paged_attention_wrapper, backend="openxla")

    for attn_logits_soft_cap in (1.0, None):
      output = compiled_paged_attention(
          q_xla,
          k_pages_xla,
          v_pages_xla,
          kv_seq_lens_xla,
          page_indices_xla,
          effective_q_lens_xla,
          num_kv_pages_per_compute_block=block_kv_size // page_size,
          num_queries_per_compute_block=num_queries_per_compute_block,
          use_kernel=True,
          attn_logits_soft_cap=attn_logits_soft_cap,
      )

      nonkernel_output = compiled_paged_attention(
          q_xla,
          k_pages_xla,
          v_pages_xla,
          kv_seq_lens_xla,
          page_indices_xla,
          effective_q_lens_xla,
          num_kv_pages_per_compute_block=block_kv_size // page_size,
          num_queries_per_compute_block=num_queries_per_compute_block,
          use_kernel=False,
          attn_logits_soft_cap=attn_logits_soft_cap,
      )

      self.assertTrue(
          torch.allclose(
              output.cpu(), nonkernel_output.cpu(), atol=1e-2, rtol=1e-2))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() != 4,
                   "This test only works on TPUv4 and TPUv5p.")
  def test_paged_attention_wrapper_with_megacore_modes(self):
    # TODO: enable checking TPU accelerator types.
    from torch_xla.experimental.custom_kernel import paged_attention
    from jax.experimental.pallas.ops.tpu.paged_attention.paged_attention_kernel import paged_attention as jax_paged_attention

    max_kv_len = 2048
    block_size = 512
    page_size = 64
    num_kv_heads = 8
    q_kv_head_ratio = 8
    head_dim = 256
    dtype = torch.float32
    seq_lens = torch.tensor([0, 3, 256, 513, 1023, 2048], dtype=torch.int32)

    q, k_pages, v_pages, page_indices = self._pagedattention_generate_qkv(
        seq_lens,
        page_size,
        max_kv_len,
        num_kv_heads,
        num_kv_heads * q_kv_head_ratio,
        head_dim,
    )

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    seq_lens_xla = seq_lens.to("xla")
    page_indices_xla = page_indices.to("xla")

    outputs = []
    for megacore_mode in ['kv_head', 'batch', None]:
      outputs.append(
          paged_attention(
              q_xla,
              k_pages_xla,
              v_pages_xla,
              seq_lens_xla,
              page_indices_xla,
              pages_per_compute_block=block_size // page_size,
              megacore_mode=megacore_mode))

    q_jax = jnp.array(q.numpy(), dtype=jnp.float32)
    k_pages_jax = jnp.array(k_pages.numpy(), dtype=jnp.float32)
    v_pages_jax = jnp.array(v_pages.numpy(), dtype=jnp.float32)
    seq_lens_jax = jnp.array(seq_lens.numpy(), dtype=jnp.int32)
    page_indices_jax = jnp.array(page_indices.numpy(), dtype=jnp.int32)
    expected_outputs = []
    for megacore_mode in ['kv_head', 'batch', None]:
      expected_outputs.append(
          torch.from_numpy(
              np.array(
                  jax_paged_attention(
                      q_jax,
                      k_pages_jax,
                      v_pages_jax,
                      seq_lens_jax,
                      page_indices_jax,
                      pages_per_compute_block=block_size // page_size,
                      megacore_mode=megacore_mode))))

    for output, expected_output in zip(outputs, expected_outputs):
      self.assertTrue(
          torch.allclose(
              output.cpu()[seq_lens > 0],
              expected_output.cpu()[seq_lens > 0],
              atol=1e-5,
              rtol=1e-5))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  def test_paged_attention_wrapper_with_dynamo(self):
    from jax.experimental.pallas.ops.tpu.paged_attention.paged_attention_kernel import paged_attention as jax_paged_attention

    max_kv_len = 2048
    block_size = 512
    page_size = 64
    num_kv_heads = 8
    q_kv_head_ratio = 8
    head_dim = 256
    seq_lens = torch.tensor([0, 3, 256, 513, 1023, 2048], dtype=torch.int32)

    q, k_pages, v_pages, page_indices = self._pagedattention_generate_qkv(
        seq_lens,
        page_size,
        max_kv_len,
        num_kv_heads,
        num_kv_heads * q_kv_head_ratio,
        head_dim,
    )

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    seq_lens_xla = seq_lens.to("xla")
    page_indices_xla = page_indices.to("xla")

    def paged_attention_wrapper(q, k, v, seq_lens, page_indices,
                                pages_per_compute_block, attn_logits_soft_cap):
      return torch.ops.xla.paged_attention(
          q,
          k,
          v,
          seq_lens,
          page_indices,
          pages_per_compute_block=pages_per_compute_block,
          attn_logits_soft_cap=attn_logits_soft_cap,
      )

    compiled_paged_attention = torch.compile(
        paged_attention_wrapper, backend="openxla")

    q_jax = jnp.array(q.numpy(), dtype=jnp.float32)
    k_pages_jax = jnp.array(k_pages.numpy(), dtype=jnp.float32)
    v_pages_jax = jnp.array(v_pages.numpy(), dtype=jnp.float32)
    seq_lens_jax = jnp.array(seq_lens.numpy(), dtype=jnp.int32)
    page_indices_jax = jnp.array(page_indices.numpy(), dtype=jnp.int32)

    for attn_logits_soft_cap in (1.0, None):
      output = compiled_paged_attention(
          q_xla,
          k_pages_xla,
          v_pages_xla,
          seq_lens_xla,
          page_indices_xla,
          pages_per_compute_block=block_size // page_size,
          attn_logits_soft_cap=attn_logits_soft_cap,
      )
      expected_output = torch.from_numpy(
          np.array(
              jax_paged_attention(
                  q_jax,
                  k_pages_jax,
                  v_pages_jax,
                  seq_lens_jax,
                  page_indices_jax,
                  pages_per_compute_block=block_size // page_size,
                  attn_logits_soft_cap=attn_logits_soft_cap,
              )))

      self.assertTrue(
          torch.allclose(
              output.cpu()[seq_lens > 0],
              expected_output.cpu()[seq_lens > 0],
              atol=1e-5,
              rtol=1e-5))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  def test_paged_attention_wrapper_with_attn_logits_soft_cap(self):
    # TODO: enable checking TPU accelerator types.
    from torch_xla.experimental.custom_kernel import paged_attention
    from jax.experimental.pallas.ops.tpu.paged_attention.paged_attention_kernel import paged_attention as jax_paged_attention

    max_kv_len = 2048
    block_size = 512
    page_size = 64
    num_kv_heads = 8
    q_kv_head_ratio = 8
    head_dim = 256
    seq_lens = torch.tensor([0, 3, 256, 513, 1023, 2048], dtype=torch.int32)

    q, k_pages, v_pages, page_indices = self._pagedattention_generate_qkv(
        seq_lens,
        page_size,
        max_kv_len,
        num_kv_heads,
        num_kv_heads * q_kv_head_ratio,
        head_dim,
    )

    q_xla = q.to("xla")
    k_pages_xla = k_pages.to("xla")
    v_pages_xla = v_pages.to("xla")
    seq_lens_xla = seq_lens.to("xla")
    page_indices_xla = page_indices.to("xla")

    outputs = []
    for attn_logits_soft_cap in [1.0, None]:
      outputs.append(
          paged_attention(
              q_xla,
              k_pages_xla,
              v_pages_xla,
              seq_lens_xla,
              page_indices_xla,
              pages_per_compute_block=block_size // page_size,
              attn_logits_soft_cap=attn_logits_soft_cap))

    q_jax = jnp.array(q.numpy(), dtype=jnp.float32)
    k_pages_jax = jnp.array(k_pages.numpy(), dtype=jnp.float32)
    v_pages_jax = jnp.array(v_pages.numpy(), dtype=jnp.float32)
    seq_lens_jax = jnp.array(seq_lens.numpy(), dtype=jnp.int32)
    page_indices_jax = jnp.array(page_indices.numpy(), dtype=jnp.int32)
    expected_outputs = []
    for attn_logits_soft_cap in [1.0, None]:
      expected_outputs.append(
          torch.from_numpy(
              np.array(
                  jax_paged_attention(
                      q_jax,
                      k_pages_jax,
                      v_pages_jax,
                      seq_lens_jax,
                      page_indices_jax,
                      pages_per_compute_block=block_size // page_size,
                      attn_logits_soft_cap=attn_logits_soft_cap))))

    for output, expected_output in zip(outputs, expected_outputs):
      self.assertTrue(
          torch.allclose(
              output.cpu()[seq_lens > 0],
              expected_output.cpu()[seq_lens > 0],
              atol=1e-5,
              rtol=1e-5))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  def test_flash_attention_wrapper_segment_ids_1(self):
    from torch_xla.experimental.custom_kernel import flash_attention
    from jax.experimental.pallas.ops.tpu.flash_attention import flash_attention as jax_flash_attention, SegmentIds

    q = torch.randn(3, 2, 128, 4)
    k = torch.randn(3, 2, 128, 4)
    v = torch.randn(3, 2, 128, 4)
    zeros = torch.zeros(3, 32)
    segment_ids = torch.cat([zeros, zeros + 1, zeros + 2, zeros + 3], dim=1)
    o = flash_attention(
        q.to("xla"), k.to("xla"), v.to("xla"), False, segment_ids.to("xla"),
        segment_ids.to("xla"))

    jax_q = jnp.array(q.numpy(), dtype=jnp.float32)
    jax_k = jnp.array(k.numpy(), dtype=jnp.float32)
    jax_v = jnp.array(v.numpy(), dtype=jnp.float32)
    jax_segment_ids = jnp.array(segment_ids.numpy(), dtype=jnp.float32)
    expected_o = torch.from_numpy(
        np.array(
            jax_flash_attention(
                jax_q,
                jax_k,
                jax_v,
                segment_ids=SegmentIds(jax_segment_ids, jax_segment_ids),
            )))

    self.assertTrue(torch.allclose(o.cpu(), expected_o.cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_wrapper_segment_ids_2(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")
    zeros = torch.zeros(3, 32).to("xla")
    segment_ids = torch.cat([zeros, zeros + 1, zeros + 2, zeros + 3], dim=1)
    o = flash_attention(q, k, v, False, segment_ids, segment_ids)

    expected_o = self._attention(
        q,
        k,
        v,
        attn_mask=self._make_attention_mask_from_segment_ids(
            segment_ids, segment_ids))
    self.assertTrue(torch.allclose(o.cpu(), expected_o.cpu(), atol=1e-05))
    jax.config.update("jax_default_matmul_precision", "default")

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  def test_flash_attention_backward_segment_ids(self):
    jax.config.update("jax_default_matmul_precision", "highest")
    from torch_xla.experimental.custom_kernel import flash_attention

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    zeros = torch.zeros(4, 32).to("xla")
    segment_ids = torch.cat([zeros, zeros + 1, zeros + 2, zeros + 3], dim=1)
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()

    o = flash_attention(q, k, v, False, segment_ids, segment_ids)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    q_grad = q.grad
    k_grad = k.grad
    v_grad = v.grad

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    zeros = torch.zeros(4, 32).to("xla")
    segment_ids = torch.cat([zeros, zeros + 1, zeros + 2, zeros + 3], dim=1)
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()

    o = self._attention(
        q,
        k,
        v,
        attn_mask=self._make_attention_mask_from_segment_ids(
            segment_ids, segment_ids))
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    for i in [(q, q_grad), (k, k_grad), (v, v_grad)]:
      self.assertTrue(torch.allclose(i[0].grad.cpu(), i[1].cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_wrapper_sm_scale(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")
    sm_scale = 0.7
    o = flash_attention(q, k, v, False, None, None, sm_scale)

    expected_o = self._attention(q * sm_scale, k, v)
    self.assertTrue(torch.allclose(o.cpu(), expected_o.cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_sm_scale_backward(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    sm_scale = 0.7
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()

    o = flash_attention(q, k, v, False, None, None, sm_scale)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    q_grad = q.grad
    k_grad = k.grad
    v_grad = v.grad

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()

    o = self._attention(q * sm_scale, k, v)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    # Hmm, the gradients are the same even the autograd graph seems different.
    for i in [(q, q_grad), (k, k_grad), (v, v_grad)]:
      self.assertTrue(torch.allclose(i[0].grad.cpu(), i[1].cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_ab(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    q = torch.randn(3, 2, 128, 4).to("xla")
    k = torch.randn(3, 2, 128, 4).to("xla")
    v = torch.randn(3, 2, 128, 4).to("xla")
    mask = (torch.rand(3, 2, 128, 128) > 0.5).to("xla")
    ab = torch.ones(3, 2, 128, 128).to("xla")
    ab = ab.masked_fill(mask, torch.finfo(ab.dtype).min)
    o = flash_attention(q, k, v, ab=ab)

    expected_o = self._attention(q, k, v, ab=ab)
    self.assertTrue(torch.allclose(o.cpu(), expected_o.cpu(), atol=1e-05))
    jax.config.update("jax_default_matmul_precision", "default")

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  def test_flash_attention_ab_backward_1(self):
    jax.config.update("jax_default_matmul_precision", "highest")
    from torch_xla.experimental.custom_kernel import flash_attention

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    mask = (torch.rand(4, 2, 128, 128) > 0.5).to("xla")
    ab = torch.ones(4, 2, 128, 128).to("xla")
    ab = ab.masked_fill(mask, torch.finfo(ab.dtype).min)
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()

    o = flash_attention(q, k, v, ab=ab)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    q_grad = q.grad
    k_grad = k.grad
    v_grad = v.grad

    q.grad = None
    k.grad = None
    v.grad = None

    o = self._attention(q, k, v, ab=ab)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    for i in [(q, q_grad), (k, k_grad), (v, v_grad)]:
      self.assertTrue(torch.allclose(i[0].grad.cpu(), i[1].cpu(), atol=1e-05))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 3,
                   "This test only works on TPUv3+.")
  @with_jax_high_precision
  def test_flash_attention_ab_backward_2(self):
    from torch_xla.experimental.custom_kernel import flash_attention

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    mask = (torch.rand(4, 2, 128, 128) > 0.5).to("xla")
    ab = torch.ones(4, 2, 128, 128).to("xla")
    ab = ab.masked_fill(mask, torch.finfo(ab.dtype).min)
    ab.requires_grad = True
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()
    ab.retain_grad()

    o = flash_attention(q, k, v, ab=ab)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    q_grad = q.grad
    k_grad = k.grad
    v_grad = v.grad
    ab_grad = ab.grad

    q.grad = None
    k.grad = None
    v.grad = None
    ab.grad = None

    o = self._attention(q, k, v, ab=ab)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    for i in [(q, q_grad), (k, k_grad), (v, v_grad), (ab, ab_grad)]:
      self.assertTrue(torch.allclose(i[0].grad.cpu(), i[1].cpu(), atol=1e-05))

  @parameterized.named_parameters(('off', False), ('on', True))
  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  @with_jax_high_precision
  def test_flash_attention_forward_aot_autograd_traceable_causal(self, causal):
    from functorch.compile import aot_function, make_boxed_func
    from torch_xla.experimental.custom_kernel import flash_attention
    import torch_xla.core.xla_model as xm

    def compiler(gm, _):
      return make_boxed_func(gm)

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()
    B, N, SEQ, H = q.size()
    q_segment_ids = None
    kv_segment_ids = None
    sm_scale = 1.0

    compiled_flash_attention = aot_function(
        flash_attention, fw_compiler=compiler)
    o_actual = compiled_flash_attention(q, k, v, causal, q_segment_ids,
                                        kv_segment_ids, sm_scale)
    xm.mark_step()
    if causal:
      attention_mask = torch.triu(torch.ones(SEQ, SEQ), diagonal=1).to("xla")
    else:
      attention_mask = None

    expected_output = self._attention(q, k, v, attn_mask=attention_mask)
    xm.mark_step()
    self.assertTrue(
        torch.allclose(o_actual.cpu(), expected_output.cpu(), atol=1e-5))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  @with_jax_high_precision
  def test_flash_attention_forward_aot_autograd_traceable_ab(self):
    from functorch.compile import aot_function, make_boxed_func
    from torch_xla.experimental.custom_kernel import flash_attention
    import torch_xla.core.xla_model as xm

    def compiler(gm, _):
      return make_boxed_func(gm)

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8).to("xla")
    k = torch.randn(4, 2, 128, 8).to("xla")
    v = torch.randn(4, 2, 128, 8).to("xla")
    B, N, SEQ, H = q.size()
    causal = False
    q_segment_ids = None
    kv_segment_ids = None
    sm_scale = 1.0
    mask = (torch.rand(4, 2, 128, 128) > 0.5).to("xla")
    ab = torch.ones(4, 2, 128, 128).to("xla")
    ab = ab.masked_fill(mask, torch.finfo(ab.dtype).min)

    compiled_flash_attention = aot_function(
        flash_attention, fw_compiler=compiler)
    o_actual = compiled_flash_attention(
        q, k, v, causal, q_segment_ids, kv_segment_ids, sm_scale, ab=ab)
    xm.mark_step()

    expected_output = self._attention(q, k, v, ab=ab)
    xm.mark_step()
    self.assertTrue(
        torch.allclose(o_actual.cpu(), expected_output.cpu(), atol=1e-5))

  @unittest.skipIf(xr.device_type() != 'TPU' or tpu.version() < 4,
                   "This test only works on TPUv4+.")
  @with_jax_high_precision
  def test_flash_attention_backward_aot_autograd_traceable(self):
    from functorch.compile import aot_function, make_boxed_func
    from torch_xla.experimental.custom_kernel import flash_attention
    import torch_xla.core.xla_model as xm

    def compiler(gm, _):
      return make_boxed_func(gm)

    torch.manual_seed(42)
    q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    q.retain_grad()
    k.retain_grad()
    v.retain_grad()
    B, N, SEQ, H = q.size()
    mask = (torch.rand(4, 2, 128, 128) > 0.5).to("xla")
    ab = torch.ones(4, 2, 128, 128).to("xla")
    ab = ab.masked_fill(mask, torch.finfo(ab.dtype).min).requires_grad_()
    ab.retain_grad()

    causal = False
    q_segment_ids = None
    kv_segment_ids = None
    sm_scale = 1.0
    compiled_flash_attention = aot_function(
        flash_attention, fw_compiler=compiler)
    o_actual = compiled_flash_attention(
        q, k, v, causal, q_segment_ids, kv_segment_ids, sm_scale, ab=ab)
    loss = o_actual.sum()
    loss.backward()
    xm.mark_step()
    q_grad = q.grad
    k_grad = k.grad
    v_grad = v.grad
    ab_grad = ab.grad

    torch.manual_seed(42)
    expected_q = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    expected_k = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    expected_v = torch.randn(4, 2, 128, 8, requires_grad=True).to("xla")
    expected_q.retain_grad()
    expected_k.retain_grad()
    expected_v.retain_grad()
    expected_ab = torch.ones(4, 2, 128, 128).to("xla")
    expected_ab = expected_ab.masked_fill(mask,
                                          torch.finfo(
                                              ab.dtype).min).requires_grad_()
    expected_ab.retain_grad()
    o = self._attention(expected_q, expected_k, expected_v, ab=expected_ab)
    loss = o.sum()
    loss.backward()
    xm.mark_step()

    for expected_tensor, actual_tensor_grad in [(expected_q, q_grad),
                                                (expected_k, k_grad),
                                                (expected_v, v_grad),
                                                (expected_ab, ab_grad)]:
      self.assertTrue(
          torch.allclose(
              expected_tensor.grad.cpu(), actual_tensor_grad.cpu(), atol=1e-02))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  torch.set_default_dtype(torch.float32)
  torch.manual_seed(42)
  torch_xla._XLAC._xla_set_mat_mul_precision('highest')
  test = unittest.main()
  sys.exit(0 if test.result.wasSuccessful() else 1)
