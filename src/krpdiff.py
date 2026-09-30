# Apply HDiffPatch

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

MAGIC_VERSION = b"HDIFF19"

__all__ = [
    "KrpdiffError",
    "OldRefFile",
    "NewRefFile",
    "CopyFile",
    "DirPatch",
    "parse_dir_patch",
    "apply_group_patch",
    "looks_like_dir_patch",
]

class KrpdiffError(Exception):
    """frpdiff 解析/应用失败"""

@dataclass
class OldRefFile:
    old_path: str
    old_size: Optional[int] = None

@dataclass
class NewRefFile:
    new_path: str
    new_size: int

@dataclass
class CopyFile:
    old_path: str
    new_path:str

@dataclass
class DirPatch:
    """解析后的目录补丁"""
    version: str
    compress_type: str # 目录头的压缩类型，一般zstd
    checksum_type: str # 校验类型，跳过
    old_paths: Tuple[str, ...] # 旧路径
    new_paths: Tuple[str, ...] # 新路径
    old_ref_files: Tuple[OldRefFile, ...] # 旧引用文件，旧流拼接顺序
    new_ref_files: Tuple[NewRefFile, ...] # 新引用文件
    copy_files: Tuple[CopyFile, ...] # 复制类文件
    execute_files: Tuple[str, ...] # 需要补可执行位的新文件路径
    new_dirs: Tuple[str, ...] # 新目录
    empty_files: Tuple[str, ...] # 空文件
    old_stream_size: int # 旧引用文件合并总大小，用于校验
    new_stream_size: int # 新引用文件合并总大小
    _data: bytes = field(default=b"", repr=False) # 原始补丁数据
    _hdiff_offset: int = field(default=0, repr=False) # 差分流起始偏移

    def apply(self, read_old: Callable[[str], bytes]) -> Iterator[Tuple[str, bytes]]:
        old_stream = self._build_old_stream(read_old)
        new_stream = self._apply_hdiff_stream(old_stream)
        if len(new_stream) != self.new_stream_size:
            raise KrpdiffError(
                "差分流输出大小错误: 实际 %d 字节, 期望 %d 字节"
                % (len(new_stream), self.new_stream_size)
            )
        pos = 0
        for ref in self.new_ref_files:
            yield ref.new_path, new_stream[pos:pos + ref.new_size]
            pos += ref.new_size

    def _build_old_stream(self, read_old: Callable[[str], bytes]) -> bytes:
        parts: List[bytes] = []
        total = 0
        for ref in self.old_ref_files:
            data = read_old(ref.old_path)
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise TypeError("must bytes: %r" % (ref.old_path,))
            data = bytes(data)
            if (ref.old_size is not None) and (len(data) != ref.old_size):
                raise KrpdiffError(
                    "旧文件大小错误: %s 实际 %d 字节, 期望 %d 字节"
                    % (ref.old_path, len(data), ref.old_size)
                )
            parts.append(data)
            total += len(data)
        if total != self.old_stream_size:
            raise KrpdiffError(
                "旧文件总大小错误: 实际 %d 字节, 补丁要求 %d 字节"
                % (total, self.old_stream_size)
            )
        return b"".join(parts)

    def _apply_hdiff_stream(self, old_stream: bytes) -> bytes:
        hdiffpatch = _import_hdiffpatch()
        diff_stream = self._data[self._hdiff_offset:]
        try:
            return hdiffpatch.apply(old_stream, diff_stream)
        except Exception as e: # hdiffpatch.HDiffPatchError
            raise KrpdiffError("apply diff failed: %s" % (e,)) from e

def parse_dir_patch(data: bytes) -> DirPatch:
    """解析 krpdiff 目录补丁"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("must bytes")
    data = bytes(data)
    if not looks_like_dir_patch(data):
        raise KrpdiffError("not dir patch")
    pos = 0
    # header "HDIFF19&{compressType}&{checksumType}\0"
    end = data.find(b"&", pos)
    if end < 0:
        raise KrpdiffError("broken header: missing version")
    version = data[pos:end].decode("ascii", "replace")
    if data[pos:end] != MAGIC_VERSION:
        raise KrpdiffError("unsupported patch version: %r" % (version,))
    pos = end + 1
    end = data.find(b"&", pos)
    if end < 0:
        raise KrpdiffError("broken header: missing compression type")
    compress_type = data[pos:end].decode("ascii", "replace")
    pos = end + 1
    end = data.find(b"\0", pos)
    if end < 0:
        raise KrpdiffError("broken header: missing check terminator")
    checksum_type = data[pos:end].decode("ascii", "replace")
    pos = end + 1
    def rd(tag_bits: int = 0) -> Tuple[int, int]:
        nonlocal pos
        value, pos, tag = _read_uint(data, pos, tag_bits)
        return value, tag
    # 目录补丁头部字段
    # 参考HDiffPatch dir_patch.c
    rd() # oldPathIsDir
    rd() # newPathIsDir
    old_path_count, _ = rd()
    old_path_sum_size, _ = rd()
    new_path_count, _ = rd()
    new_path_sum_size, _ = rd()
    old_ref_count, _ = rd()
    saved_old_ref_size, _ = rd() # 旧引用文件合并总大小
    new_ref_count, _ = rd()
    saved_new_ref_size, _ = rd() # 新引用文件合并总大小
    same_pair_count, _ = rd()
    rd()  # sameFileSize
    new_execute_count, _ = rd()
    rd()  # privateReservedDataSize, kuro似乎写错了
    private_extern_size, _ = rd()
    extern_size, _ = rd()
    head_data_size, _ = rd()
    head_data_compressed_size, _ = rd()
    checksum_byte_size, _ = rd()
    # 4组校验和
    pos += checksum_byte_size * 4
    if pos > len(data):
        raise KrpdiffError("patch header truncation")
    # 数据区偏移：目录头 -> 私有数据 -> 外部数据 -> 差分流
    head_data_offset = pos
    pos += head_data_compressed_size if head_data_compressed_size else head_data_size
    pos += private_extern_size
    pos += extern_size
    hdiff_offset = pos
    if hdiff_offset >= len(data):
        raise KrpdiffError("patch data truncated: missing diff stream")
    # 解压并解析目录头
    head_raw_size = head_data_compressed_size or head_data_size
    head_raw = data[head_data_offset:head_data_offset + head_raw_size]
    head = _decompress_dir_head(head_raw, head_data_size, head_data_compressed_size, compress_type)
    path_sum_size = old_path_sum_size + new_path_sum_size
    if path_sum_size > len(head):
        raise KrpdiffError("dir header truncated: path table oob")
    strings: List[str] = []
    blob = head[:path_sum_size]
    p = 0
    while p < path_sum_size:
        q = blob.find(b"\0", p)
        if q < 0:
            raise KrpdiffError("path table missing terminator")
        strings.append(blob[p:q].decode("utf-8"))
        p = q + 1
    if len(strings) != old_path_count + new_path_count:
        raise KrpdiffError(
            "路径数量错误: 实际 %d, 期望 %d" % (len(strings), old_path_count + new_path_count)
        )
    old_paths = tuple(strings[:old_path_count])
    new_paths = tuple(strings[old_path_count:])
    # 文件索引与列表
    p = path_sum_size
    old_ref_indices, p = _read_inc_list(head, p, old_ref_count, old_path_count, "oldRefList")
    new_ref_indices, p = _read_inc_list(head, p, new_ref_count, new_path_count, "newRefList")
    p_after_new_ref = p
    # krpdiff多一份表
    old_ref_sizes: Optional[List[int]] = None
    new_ref_sizes: Optional[List[int]] = None
    try:
        cand_old, p2 = _read_size_list(head, p, old_ref_count)
        cand_new, p3 = _read_size_list(head, p2, new_ref_count)
        if sum(cand_old) == saved_old_ref_size and sum(cand_new) == saved_new_ref_size:
            old_ref_sizes, new_ref_sizes, p = cand_old, cand_new, p3
    except KrpdiffError:
        pass
    if new_ref_sizes is None:
        p = p_after_new_ref
        new_ref_sizes, p = _read_size_list(head, p, new_ref_count)
        if sum(new_ref_sizes) != saved_new_ref_size:
            raise KrpdiffError("size mismatch")
    same_pairs: List[Tuple[int, int]] = [] # (newIndex, oldIndex)
    back_new = -1
    back_old = -1
    for _ in range(same_pair_count):
        inc_new, p, _ = _read_uint(head, p)
        back_new += 1 + inc_new
        if not (-1 < back_new < new_path_count):
            raise KrpdiffError("new index oob")
        inc_old, p, sign = _read_uint(head, p, 1)
        back_old = back_old + 1 + inc_old if sign == 0 else back_old + 1 - inc_old
        if not (-1 < back_old < old_path_count):
            raise KrpdiffError("new index oob")
        same_pairs.append((back_new, back_old))
    execute_indices, p = _read_inc_list(head, p, new_execute_count, new_path_count, "newExecuteList")
    # 跳过私有数据尾部
    old_ref_files: List[OldRefFile] = []
    for index, old_index in enumerate(old_ref_indices):
        old_rel = old_paths[old_index]
        if (not old_rel) or old_rel.endswith("/"):
            raise KrpdiffError("old ref path error: %r" % (old_rel,))
        old_size = old_ref_sizes[index] if old_ref_sizes is not None else None
        old_ref_files.append(OldRefFile(old_rel, old_size))
    new_ref_files: List[NewRefFile] = []
    for new_index, size in zip(new_ref_indices, new_ref_sizes):
        new_rel = new_paths[new_index]
        if (not new_rel) or new_rel.endswith("/"):
            raise KrpdiffError("new ref path error: %r" % (new_rel,))
        new_ref_files.append(NewRefFile(new_rel, size))
    copy_files = tuple(
        CopyFile(old_paths[old_index], new_paths[new_index]) for new_index, old_index in same_pairs
    )
    ref_new_set = set(new_ref_indices)
    copy_new_set = {pair[0] for pair in same_pairs}
    new_dirs: List[str] = []
    empty_files: List[str] = []
    for index, rel in enumerate(new_paths):
        if (index in ref_new_set) or (index in copy_new_set):
            continue
        if (rel == "") or rel.endswith("/"):
            new_dirs.append(rel)
        else:
            empty_files.append(rel)
    execute_files = tuple(new_paths[i] for i in execute_indices)
    return DirPatch(
        version=version,
        compress_type=compress_type,
        checksum_type=checksum_type,
        old_paths=old_paths,
        new_paths=new_paths,
        old_ref_files=tuple(old_ref_files),
        new_ref_files=tuple(new_ref_files),
        copy_files=copy_files,
        execute_files=execute_files,
        new_dirs=tuple(new_dirs),
        empty_files=tuple(empty_files),
        old_stream_size=saved_old_ref_size,
        new_stream_size=saved_new_ref_size,
        _data=data,
        _hdiff_offset=hdiff_offset,
    )

def apply_group_patch(patch: bytes, read_old: Callable[[str], bytes]) -> Dict[str, bytes]:
    """完整还原一个组补丁，返回新文件路径与内容"""
    patch_info = parse_dir_patch(patch)
    result: Dict[str, bytes] = {}
    for new_path, content in patch_info.apply(read_old):
        result[new_path] = content
    for copy in patch_info.copy_files:
        result[copy.new_path] = bytes(read_old(copy.old_path))
    for path in patch_info.empty_files:
        result[path] = b""
    return result

def looks_like_dir_patch(data) -> bool:
    """快速判断 krpdiff/HDIFF19 类patch"""
    return (
        isinstance(data, (bytes, bytearray, memoryview)) and bytes(data[:7]) == MAGIC_VERSION
    )

def _read_uint(data: bytes, pos: int, tag_bits: int = 0) -> Tuple[int, int, int]:
    """读取 HDiffPatch 变长整数，对应 ``unpackUIntWithTag``"""
    if pos >= len(data):
        raise KrpdiffError("data oob")
    first = data[pos]
    pos += 1
    value = first & ((1 << (7 - tag_bits)) - 1)
    if first & (1 << (7 - tag_bits)):
        while True:
            if pos >= len(data):
                raise KrpdiffError("data oob")
            code = data[pos]
            pos += 1
            value = (value << 7) | (code & 0x7F)
            if not (code & 0x80):
                break
    tag = (first >> (8 - tag_bits)) if tag_bits else 0
    return value, pos, tag

def _read_inc_list(data: bytes, pos: int, count: int, end_value: int, what: str) -> Tuple[List[int], int]:
    """读取递增索引表"""
    out: List[int] = []
    back = -1
    for _ in range(count):
        inc, pos, _ = _read_uint(data, pos)
        back += 1 + inc
        if not (-1 < back < end_value):
            raise KrpdiffError("%s index oob" % what)
        out.append(back)
    return out, pos

def _read_size_list(data: bytes, pos: int, count: int) -> Tuple[List[int], int]:
    """读取文件size表"""
    out: List[int] = []
    for _ in range(count):
        value, pos, _ = _read_uint(data, pos)
        out.append(value)
    return out, pos

def _decompress_dir_head(raw: bytes, uncompressed_size: int, compressed_size: int, compress_type: str) -> bytes:
    if compressed_size == 0:
        return raw
    if compress_type != "zstd":
        raise KrpdiffError("unsupported compression type: %r" % (compress_type,))
    out = _zstd_decompress(raw, uncompressed_size)
    if len(out) != uncompressed_size:
        raise KrpdiffError(
            "目录头解压后大小错误: 实际 %d 字节, 期望 %d 字节" % (len(out), uncompressed_size)
        )
    return out

def _zstd_decompress(data: bytes, max_size: int) -> bytes:
    try:
        from compression import zstd as _zstd # Python 3.14+
    except ImportError:
        _zstd = None
    if _zstd is not None:
        try:
            return _zstd.decompress(data)
        except Exception:
            pass
    try:
        import zstandard
    except ImportError as e:
        raise KrpdiffError("missing zstandard module") from e
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=max_size)

def _import_hdiffpatch():
    try:
        import hdiffpatch
    except ImportError as e:
        raise KrpdiffError("missing hdiffpatch 2.4+ module") from e
    return hdiffpatch
