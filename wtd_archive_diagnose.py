#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读 WTD 归档查漏清单：三个分页、紧凑表格、完整 URL。

放在 download_archive.py 同目录，按 WTD 的惯例从该目录运行：
    python wtd_archive_diagnose.py 用户名
    python wtd_archive_diagnose.py 用户名 --output D:\\reports\\检查.html

不访问网络，不修改任何原始归档文件；默认在用户归档目录（USERNAME_archive）生成一份 HTML，也可用 --output 指定位置。
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
import json
from pathlib import Path
import re
import sys
from typing import Any

ID = re.compile(r"^\d{5,25}$")
USER = re.compile(r"^[A-Za-z0-9_]{1,15}$")
STAMP = re.compile(r"^\d{14}$")
FILE = re.compile(r"^(\d{14})_(\d{5,25})\.json$", re.I)
MAX_DEPTH = 64


def mapping(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def listing(x: Any) -> list:
    return x if isinstance(x, list) else []


def string(x: Any) -> str:
    return str(x).strip() if x is not None else ''


def ident(x: Any) -> str:
    s = string(x)
    return s if ID.fullmatch(s) else ''


def read_json(path: Path) -> Any:
    try:
        with path.open('r', encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def node_id(node: dict) -> str:
    legacy = mapping(node.get('legacy'))
    return ident(node.get('id') or node.get('rest_id') or node.get('id_str') or legacy.get('id_str'))


def parent_id(node: dict) -> str:
    legacy = mapping(node.get('legacy'))
    for ref in listing(node.get('referenced_tweets')):
        if mapping(ref).get('type') == 'replied_to':
            tid = ident(mapping(ref).get('id'))
            if tid:
                return tid
    return ident(node.get('in_reply_to_status_id_str') or node.get('in_reply_to_status_id')
                 or legacy.get('in_reply_to_status_id_str') or legacy.get('in_reply_to_status_id'))


def conversation_id(node: dict) -> str:
    legacy = mapping(node.get('legacy'))
    return ident(node.get('conversation_id') or node.get('conversation_id_str')
                 or legacy.get('conversation_id_str') or legacy.get('conversation_id'))


def created_at(node: dict) -> str:
    return string(node.get('created_at') or mapping(node.get('legacy')).get('created_at'))


def tweet_text(node: dict) -> str:
    legacy = mapping(node.get('legacy'))
    note = mapping(mapping(mapping(node.get('note_tweet')).get('note_tweet_results')).get('result'))
    return string(note.get('text') or mapping(node.get('note_tweet')).get('text')
                  or node.get('full_text') or node.get('text') or legacy.get('full_text') or legacy.get('text'))


def has_media(node: dict) -> bool:
    legacy = mapping(node.get('legacy'))
    if listing(mapping(node.get('attachments')).get('media_keys')):
        return True
    if listing(mapping(legacy.get('extended_entities')).get('media')):
        return True
    if listing(mapping(legacy.get('entities')).get('media')):
        return True
    if listing(mapping(node.get('extended_entities')).get('media')):
        return True
    if listing(mapping(node.get('entities')).get('media')):
        return True
    return False


def author_evidence(node: dict, payload: dict | None = None) -> bool:
    legacy = mapping(node.get('legacy'))
    core = mapping(mapping(mapping(node.get('core')).get('user_results')).get('result'))
    user = mapping(node.get('user'))
    uid = ident(node.get('author_id') or legacy.get('user_id_str') or core.get('rest_id')
                or user.get('id_str') or user.get('id'))
    if uid:
        return True
    if string(legacy.get('screen_name') or node.get('screen_name') or user.get('screen_name')):
        return True
    return False


def usable(node: dict, expected: str = '', payload: dict | None = None) -> bool:
    if not isinstance(node, dict):
        return False
    tid = node_id(node)
    if not tid or (expected and tid != expected):
        return False
    # 正文或真实媒体必须至少有一种。没有内容的占位、错误响应不算真实推文。
    if not tweet_text(node) and not has_media(node):
        return False
    # ID + 内容不足以排除含假 id 的错误对象；至少要有一项真实推文元数据。
    return bool(author_evidence(node, payload) or created_at(node))


def primary_node(payload: dict, wanted: str, core) -> dict | None:
    # 最常见的 v2 独立 JSON，严格要求 data.id 与文件/清单 ID 一致。
    data = mapping(payload.get('data'))
    if node_id(data) == wanted:
        return data
    # WTD 的 GraphQL / Wayback 旧格式由原工具解析，仍强制 ID 相符。
    try:
        found = core.find_primary_tweet_node(payload, core.reconstruct_original_url('i', wanted))
        if isinstance(found, dict) and node_id(found) == wanted:
            return found
    except (AttributeError, TypeError, ValueError, KeyError):
        pass
    return None


def root_proven(node: dict) -> bool:
    if parent_id(node):
        return False
    tid = node_id(node)
    conv = conversation_id(node)
    # 对有会话 ID 的节点，只有会话 ID = 本推文 ID 才是根。
    if conv:
        return conv == tid
    # 无 conversation_id 的老式真实推文，只有明确给出了无回复标记才可能确认。
    if 'referenced_tweets' in node and listing(node.get('referenced_tweets')):
        return all(mapping(r).get('type') != 'replied_to' for r in listing(node['referenced_tweets']))
    legacy = mapping(node.get('legacy'))
    for container in (node, legacy):
        if ('in_reply_to_status_id_str' in container or 'in_reply_to_status_id' in container):
            return True
    return False


def extracts_embedded(payload: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """返回当前独立 JSON 内的有效嵌入推文，以及有 ID 但内容无效的嵌入项。

    只遍历 includes.tweets、WTD 缓存的 posts.payload，不递归任意字段，
    防止误把无关 quoted/user 对象当成上级，也避免恶性嵌套扫描。
    """
    good: dict[str, dict] = {}
    bad: dict[str, dict] = {}
    todo: list[tuple[dict, int]] = [(payload, 0)]
    seen: set[int] = set()
    while todo:
        current, depth = todo.pop()
        if depth > 16 or id(current) in seen or len(seen) > 50000:
            continue
        seen.add(id(current))
        inc = mapping(current.get('includes'))
        for raw in listing(inc.get('tweets')):
            node = mapping(raw)
            tid = node_id(node)
            if tid:
                (good if usable(node, tid, current) else bad).setdefault(tid, node)
        pf = mapping(current.get('_pale_fire'))
        chains = mapping(pf.get('x_api_reply_chains'))
        for entry in chains.values():
            for item in listing(mapping(entry).get('posts')):
                item = mapping(item)
                embedded_payload = mapping(item.get('payload'))
                if not embedded_payload:
                    continue
                candidate = mapping(embedded_payload.get('data'))
                cached_id = ident(item.get('id'))
                tid = node_id(candidate)
                if tid and (not cached_id or cached_id == tid):
                    (good if usable(candidate, tid, embedded_payload) else bad).setdefault(tid, candidate)
                todo.append((embedded_payload, depth + 1))
    return good, bad


def cache_errors(payload: dict) -> set[str]:
    errors: set[str] = set()
    for entry in mapping(mapping(payload.get('_pale_fire')).get('x_api_reply_chains')).values():
        err = mapping(mapping(entry).get('terminal_error'))
        if string(err.get('message')) and ident(err.get('id')):
            errors.add(ident(err['id']))
    return errors


@dataclass
class Snapshot:
    timestamp: str
    original_url: str
    filename: str
    tid: str
    path: Path


@dataclass
class Primary:
    tid: str
    path: Path
    node: dict
    embedded: dict[str, dict]
    errors: set[str]


@dataclass
class MissingParent:
    children: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)


class DiagnoseError(RuntimeError):
    pass


def resolve_paths(core, username: str) -> tuple[Path, Path, Path]:
    _, asset_dir, _, json_dir, _ = core.build_output_paths(username)
    return asset_dir, json_dir, core.snapshot_manifest_path(asset_dir)


def load_manifest(manifest_path: Path, json_dir: Path, username: str) -> list[Snapshot]:
    if not manifest_path.is_file():
        raise DiagnoseError(f'没有找到 snapshots.json：{manifest_path}')
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('snapshots'), list):
        raise DiagnoseError('snapshots.json 无法解析或结构错误；停止诊断，避免虚假的缺失清单')
    recorded_user = string(manifest.get('user'))
    if recorded_user and recorded_user.casefold() != username.casefold():
        raise DiagnoseError(f'snapshots.json 的账号是 {recorded_user}，不是 @{username}')
    snapshots: list[Snapshot] = []
    for item in manifest['snapshots']:
        obj = mapping(item)
        filename = string(obj.get('json_filename'))
        if not filename or Path(filename).name != filename or ('\\' in filename) or ('/' in filename):
            continue
        parsed = FILE.fullmatch(filename)
        tid = parsed.group(2) if parsed else ''
        url = string(obj.get('original_url'))
        match = re.search(r'/status/(\d{5,25})(?:\D|$)', url, flags=re.I)
        if not tid and match:
            tid = match.group(1)
        if not tid:
            continue
        ts = string(obj.get('timestamp')) or (parsed.group(1) if parsed else '')
        snapshots.append(Snapshot(ts, url, filename, tid, json_dir / filename))
    return snapshots


def original_link(tid: str) -> str:
    return f'https://x.com/i/status/{tid}'


def wayback_link(tid: str, exact: Snapshot | None = None) -> str:
    if exact and STAMP.fullmatch(exact.timestamp) and exact.original_url.startswith(('https://', 'http://')):
        return f'https://web.archive.org/web/{exact.timestamp}/{exact.original_url}'
    return f'https://web.archive.org/web/*/https://twitter.com/i/status/{tid}'


def link_cell(tid: str, exact: Snapshot | None = None) -> str:
    a = original_link(tid)
    b = wayback_link(tid, exact)
    # 完整可点击 URL、竖排，绝不改成“原推文 / Wayback”文字标签。
    return ('<td class="urls">'
            f'<a href="{escape(a, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(a)}</a>'
            f'<a href="{escape(b, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(b)}</a>'
            '</td>')


def find_best_snapshot(snapshots: dict[str, list[Snapshot]], tid: str) -> Snapshot | None:
    rows = snapshots.get(tid, [])
    return next((s for s in rows if STAMP.fullmatch(s.timestamp)), None)


def diagnose(core, username: str):
    asset_dir, json_dir, manifest_path = resolve_paths(core, username)
    if not json_dir.is_dir():
        raise DiagnoseError(f'没有找到归档 JSON 目录：{json_dir}')
    snapshots = load_manifest(manifest_path, json_dir, username)
    by_tid: dict[str, list[Snapshot]] = defaultdict(list)
    for snap in snapshots:
        by_tid[snap.tid].append(snap)

    # ① 只判断文件本身是否缺失，不根据其他快照推断这条记录已归档。
    missing_snapshots: list[Snapshot] = []
    keys: set[tuple[str, str, str]] = set()
    for snap in snapshots:
        if snap.path.is_file():
            continue
        k = (snap.timestamp, snap.original_url, snap.filename)
        if k not in keys:
            missing_snapshots.append(snap)
            keys.add(k)

    # 同时检查实际存在的独立 JSON 文件，不限于清单；数据永远只读。
    all_files = sorted(json_dir.glob('*.json'))
    roots_invalid: list[tuple[str, Snapshot | None, Path]] = []
    primaries: list[Primary] = []
    globally_primary: dict[str, dict] = {}
    globally_embedded: dict[str, dict] = {}
    unattributed: list[Path] = []

    for path in all_files:
        match = FILE.fullmatch(path.name)
        if not match:
            continue
        tid = match.group(2)
        obj = read_json(path)
        if isinstance(obj, dict):
            embedded, _ = extracts_embedded(obj)
            for eid, node in embedded.items():
                globally_embedded.setdefault(eid, node)
        else:
            embedded = {}
        node = primary_node(obj, tid, core) if isinstance(obj, dict) else None
        if node is not None and usable(node, tid, obj):
            globally_primary.setdefault(tid, node)
            primaries.append(Primary(tid, path, node, embedded, cache_errors(obj)))
        else:
            # 没有真实内容的主推文无法凭空判断是不是回复。检查现存的部分字段；
            # 若不能确认是根，就不强行塞进②，避免把下级回复错误归类。
            fragment = node or mapping(mapping(obj).get('data'))
            if node_id(fragment) == tid and root_proven(fragment):
                roots_invalid.append((tid, find_best_snapshot(by_tid, tid), path))
            else:
                unattributed.append(path)

    # 即使本文件无效，也可以通过其他历史快照的同 ID 元信息确认其根身份。
    still_unknown: list[Path] = []
    for path in unattributed:
        match = FILE.fullmatch(path.name)
        tid = match.group(2) if match else ''
        known = globally_primary.get(tid) or globally_embedded.get(tid)
        if known and root_proven(known):
            roots_invalid.append((tid, find_best_snapshot(by_tid, tid), path))
        else:
            still_unknown.append(path)

    # ③ 仅处理已有有效主推文中的回复关系，不拿根推文或损坏主推文冒充回复缺链。
    missing_parents: dict[str, MissingParent] = defaultdict(MissingParent)

    def classify(missing: str, record: Primary) -> str:
        if missing in globally_primary:
            return '本地有效内容未正确嵌入，待修复回复链'
        if missing in record.embedded:
            return '当前回复链已包含，但仍有错误缓存'
        if missing in globally_embedded:
            return '本地有效内容未正确嵌入，待修复回复链'
        return '本地未找到有效上级推文'

    for post in primaries:
        child_parent = parent_id(post.node)
        if not child_parent:
            continue
        current = post.node
        seen = {post.tid}
        for _ in range(MAX_DEPTH):
            next_id = parent_id(current)
            if not next_id or next_id in seen:
                break
            seen.add(next_id)
            local_parent = post.embedded.get(next_id)
            if local_parent is not None:
                current = local_parent
                continue
            issue = missing_parents[next_id]
            issue.children.add(post.tid)
            issue.categories.add(classify(next_id, post))
            # 从全局可用的真实内容继续追溯，识别更深处的缺失；
            # 但不会把它当作已经嵌入当前 JSON。
            other = globally_primary.get(next_id) or globally_embedded.get(next_id)
            if other is None:
                break
            current = other

        # 缓存错误不能直接判定数据缺失，必须结合真实内容检查。
        for failed_id in post.errors:
            if failed_id in post.embedded:
                continue
            issue = missing_parents[failed_id]
            issue.children.add(post.tid)
            issue.categories.add(classify(failed_id, post))
            # 只有本地确实存在有效的上级内容时，旧错误缓存才是独立的
            # 引用异常；真正缺失上级时，旧失败记录不另列为问题。
            if failed_id in globally_primary or failed_id in globally_embedded:
                issue.categories.add('旧错误缓存仍引用此上级')

    return (missing_snapshots, roots_invalid, missing_parents, by_tid,
            still_unknown, len(all_files), len(snapshots))


def generate_html(username: str, result) -> str:
    missing, invalid, parents, by_tid, unknown, _, _ = result

    rows1 = ''.join(
        f'<tr><td class="id">{escape(s.tid)}</td>{link_cell(s.tid, s)}</tr>'
        for s in sorted(missing, key=lambda x: (int(x.tid), x.timestamp)))
    rows2 = ''.join(
        f'<tr><td class="id">{escape(tid)}</td>{link_cell(tid, snap)}</tr>'
        for tid, snap, path in sorted(invalid, key=lambda x: (int(x[0]), x[2].name)))

    rows3_parts = []
    for missing_id, issue in sorted(parents.items(), key=lambda kv: int(kv[0])):
        children = ', '.join(sorted(issue.children, key=int))
        categories = issue.categories
        # 多个来源中状态不一致时优先指出本地是否真的有有效推文。
        if '本地有效内容未正确嵌入，待修复回复链' in categories:
            # 同一修复动作：已有有效本地内容，但本条回复链尚未正确引用。
            # 不附加缓存说明，保持问题情况列简洁且可操作。
            reason = '本地有效内容未正确嵌入，待修复回复链'
        elif '本地未找到有效上级推文' in categories:
            reason = '本地未找到有效上级推文'
        elif '旧错误缓存仍引用此上级' in categories:
            reason = '仍有旧错误缓存'
        else:
            reason = '当前回复链已包含，但仍有错误缓存'
        rows3_parts.append(
            f'<tr><td class="id">{escape(missing_id)}</td>'
            f'{link_cell(missing_id, find_best_snapshot(by_tid, missing_id))}'
            f'<td class="state">{escape(reason)}</td>'
            f'<td class="children">{escape(children)}</td></tr>')
    rows3 = ''.join(rows3_parts)

    def table(rows: str, headers: list[str]) -> str:
        th = ''.join('<th>' + escape(h) + '</th>' for h in headers)
        return (f'<table><thead><tr>{th}</tr></thead><tbody>'
                + (rows if rows else f'<tr><td colspan="{len(headers)}" class="empty">无缺失记录</td></tr>')
                + '</tbody></table>')

    panel1 = table(rows1, ['推文 ID', '链接'])
    panel2 = table(rows2, ['推文 ID', '链接'])
    panel3 = table(rows3, ['缺失上级 ID', '链接', '问题情况', '受影响下级回复'])
    # HTML 离线、无外链 JS/CSS、无库、无网络请求；三个分页，无多余统计图。
    return '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WTD 归档诊断 - ''' + escape(username) + '''</title>
<style>
:root{color-scheme:light dark;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:13px}
body{margin:14px auto;max-width:1550px;padding:0 12px;color:light-dark(#222,#ddd);background:light-dark(#fff,#171717)}
h1{font-size:16px;font-weight:600;margin:6px 0 12px}
nav{display:flex;gap:4px;border-bottom:1px solid #999;margin-bottom:10px;flex-wrap:wrap}
nav button{appearance:none;border:1px solid #aaa;border-bottom:none;border-radius:5px 5px 0 0;background:transparent;color:inherit;padding:7px 12px;cursor:pointer;font:inherit}
nav button[aria-selected="true"]{background:light-dark(#e9eef5,#353b43);font-weight:bold}
section[hidden]{display:none}
.table-wrap{width:100%;overflow-x:auto}
table{border-collapse:collapse;width:100%;table-layout:auto;font-size:12px}
th,td{text-align:left;padding:5px 9px;border:1px solid light-dark(#ddd,#505050);vertical-align:top;line-height:1.38}
th{background:light-dark(#f0f0f0,#303030);position:sticky;top:0;white-space:nowrap}
tbody tr:nth-child(even){background:light-dark(#fafafa,#1f1f1f)}
td.id{font-family:Consolas,monospace;white-space:nowrap;width:190px}
td.urls{min-width:410px;max-width:670px;overflow-wrap:anywhere}
td.urls a{display:block;white-space:normal;color:light-dark(#0755a5,#88bbff);text-decoration:none}
td.urls a:hover{text-decoration:underline}
td.state{min-width:190px;width:240px}td.children{min-width:210px;font-family:Consolas,monospace;overflow-wrap:anywhere}
td.empty{text-align:center;color:gray;padding:14px}
</style></head><body>
<h1>WTD 归档诊断 · @''' + escape(username) + '''</h1>
<nav role="tablist" aria-label="诊断类型">
<button role="tab" type="button" aria-selected="true" aria-controls="p1" id="t1">① 快照未归档（''' + str(len(missing)) + '''）</button>
<button role="tab" type="button" aria-selected="false" aria-controls="p2" id="t2">② 本地根推文内容无效（''' + str(len(invalid)) + '''）</button>
<button role="tab" type="button" aria-selected="false" aria-controls="p3" id="t3">③ 上级回复链缺失（''' + str(len(parents)) + '''）</button>
</nav>
<section role="tabpanel" id="p1" aria-labelledby="t1"><div class="table-wrap">''' + panel1 + '''</div></section>
<section role="tabpanel" id="p2" aria-labelledby="t2" hidden><div class="table-wrap">''' + panel2 + '''</div></section>
<section role="tabpanel" id="p3" aria-labelledby="t3" hidden><div class="table-wrap">''' + panel3 + '''</div></section>
<script>
const tabs=[...document.querySelectorAll('[role="tab"]')];
for(const tab of tabs)tab.addEventListener('click',()=>{for(const other of tabs){const active=other===tab;other.setAttribute('aria-selected',String(active));document.getElementById(other.getAttribute('aria-controls')).hidden=!active;}});
</script></body></html>'''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='只读扫描 WTD 归档，输出三个分页的 HTML 缺失清单')
    parser.add_argument('username', help='WTD 账号用户名（不带 @ 亦可）')
    parser.add_argument('--output', type=Path, help='自定义诊断 HTML 输出路径（默认用户归档目录）')
    args = parser.parse_args(argv)
    name = args.username.lstrip('@')
    if not USER.fullmatch(name):
        parser.error('账号用户名格式无效')
    try:
        import download_archive as core
    except Exception as exc:
        print(f'错误：无法导入同目录的 download_archive.py：{exc}', file=sys.stderr)
        return 1
    try:
        result = diagnose(core, name)
        now = datetime.now().strftime('%Y%m%d_%H%M%S')
        # 报告默认放在该账号的 WTD 归档根目录，与主 HTML 同级。
        # 保留资源文件夹（JSON、媒体及缓存所在位置）不受报告输出影响。
        archive_dir = core.build_output_paths(name)[0].parent.resolve()
        default = archive_dir / f'WTD归档诊断_{name}_{now}.html'
        out = args.output.expanduser().resolve() if args.output else default
        asset_dir, json_dir, _ = resolve_paths(core, name)
        asset_dir, json_dir = asset_dir.resolve(), json_dir.resolve()
        if out == asset_dir or asset_dir in out.parents or out == json_dir or json_dir in out.parents:
            raise DiagnoseError('报告不能保存在 JSON、媒体或缓存资源目录内，请保存到账号归档根目录')
        if out.exists():
            raise DiagnoseError(f'报告文件已存在，拒绝覆盖：{out}')
        if not out.parent.is_dir():
            raise DiagnoseError(f'报告目标目录不存在：{out.parent}')
        out.write_text(generate_html(name, result), encoding='utf-8')
        missing, invalid, parents, _, unknown, nfiles, nsnaps = result
        print(f'检查完成：{nsnaps} 条快照清单记录、{nfiles} 份本地 JSON。')
        print(f'① 快照未归档 {len(missing)}；② 根推文内容无效 {len(invalid)}；③ 缺失上级 {len(parents)}。')
        if unknown:
            print(f'注意：{len(unknown)} 份无效 JSON 不能可靠判定是否为根推文，'
                  '为避免混入②或③，未强行分类。')
            for p in unknown[:8]:
                print('  未分类：' + p.name)
            if len(unknown) > 8:
                print(f'  其余 {len(unknown)-8} 份未分类文件未在终端展开')
        print(f'HTML：{out}')
        return 0
    except (OSError, DiagnoseError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
