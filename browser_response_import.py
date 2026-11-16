#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import already-captured X GraphQL Response JSON into a WTD archive.

Placed next to the *repaired* download_archive.py; run from its directory.
No X Developer API requests are made. Media and HTML generation delegate to WTD's existing implementation.

    python "browser_response_import.py" USER response.txt
    python "browser_response_import.py" USER responses/
    python "browser_response_import.py" scan USER response.txt
    python "browser_response_import.py" USER a.txt b.json --json-only
    python "browser_response_import.py" USER response.json --exclude-ids 1234567890123456789 2345678901234567890

Default is FULL IMPORT: write missing snapshots, fetch original media URLs,
then rebuild the complete WTD HTML bundle (five archive views + index).
Explicit `scan` or `--scan` is read-only. `--json-only` intentionally skips
media download and HTML generation.

Save the target account's captured timeline posts AND all captured descendants
of its posts as individual WTD snapshots, even when other
users authored those replies. Preserve actual author identities and reply-parent IDs.
Where a verified target-authored root is captured, also include replies with the
same conversation_id even if an intermediate parent was not loaded in the input.
When no target-authored tweet was captured, trust only verified root snapshots
already present in this target's local WTD archive. No other archive is read.
Unrelated conversations and embedded quote cards are never bulk-imported.
This script cannot discover replies absent from the supplied network Responses.
Original response files are neither changed nor removed.
Console diagnostics are concise Chinese summaries grouped by missing ancestor
or unverifiable root. Missing ancestors are checked against real, valid local
WTD primary JSON before any warning is issued. Only affected source response FILENAMES are displayed,
without full Windows paths or per-tweet field dumps. All relationships and
source paths remain preserved internally. Local parent JSON may enrich new imports.
After a successful import, existing child replies affected by NEW parent JSON
are repaired automatically (local-only).
--repair-existing-context additionally repairs Response-matched browser-imported JSON.
--exclude-ids suppresses only independent snapshot output for selected tweet IDs.
Those tweets may still be present as parent/quote context inside other JSON files;
pre-existing WTD snapshots are never deleted or altered by this option.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

USER_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
ID_RE = re.compile(r"^\d{5,25}$")
FILE_SUFFIXES = {'.json', '.txt'}


class ImportErrorSafe(RuntimeError):
    pass


def string(value: Any) -> str:
    return value if isinstance(value, str) else str(value) if value is not None else ''


def obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def sequence(value: Any) -> list:
    return value if isinstance(value, list) else []


def valid_id(value: Any) -> str:
    value = string(value).strip()
    return value if ID_RE.fullmatch(value) else ''


def iso_date(text: Any) -> str:
    value = string(text).strip()
    if not value:
        return ''
    try:
        if re.match(r'^\d{4}-\d{2}-\d{2}T', value):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            parsed = datetime.strptime(value, '%a %b %d %H:%M:%S %z %Y')
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except ValueError:
        return ''


def user_result(tweet: dict) -> dict:
    return obj(obj(obj(tweet.get('core')).get('user_results')).get('result'))


def user_id(tweet: dict) -> str:
    legacy = obj(tweet.get('legacy'))
    profile = user_result(tweet)
    return valid_id(legacy.get('user_id_str') or profile.get('rest_id'))


def user_name(tweet: dict) -> str:
    return string(obj(user_result(tweet).get('core')).get('screen_name')).strip()


def tweet_id(tweet: dict) -> str:
    return valid_id(tweet.get('rest_id') or obj(tweet.get('legacy')).get('id_str'))


def author_integrity_issue(tweet: dict) -> str:
    """Reject conflicting author and tweet identities instead of trusting one field."""
    legacy = obj(tweet.get('legacy'))
    profile = user_result(tweet)
    legacy_user = valid_id(legacy.get('user_id_str'))
    profile_user = valid_id(profile.get('rest_id'))
    raw_user = string(legacy.get('user_id_str')).strip()
    raw_profile = string(profile.get('rest_id')).strip()
    raw_tweet_id = string(legacy.get('id_str')).strip()
    outer_tweet_id = string(tweet.get('rest_id')).strip()
    if raw_user and not legacy_user:
        return 'malformed legacy.user_id_str'
    if raw_profile and not profile_user:
        return 'malformed core.user_results.result.rest_id'
    if legacy_user and profile_user and legacy_user != profile_user:
        return f'author ID mismatch (legacy={legacy_user}, profile={profile_user})'
    if raw_tweet_id and outer_tweet_id and raw_tweet_id != outer_tweet_id:
        return 'tweet ID mismatch between rest_id and legacy.id_str'
    if not user_id(tweet) or not user_name(tweet):
        return 'missing author ID or username'
    return ''


def tweet_signature(tweet: dict) -> tuple:
    """Duplicate tweet IDs must agree on relationships, not merely their text."""
    legacy = obj(tweet.get('legacy'))
    media = sequence(obj(legacy.get('extended_entities')).get('media')) or sequence(obj(legacy.get('entities')).get('media'))
    note = obj(obj(obj(tweet.get('note_tweet')).get('note_tweet_results')).get('result'))
    return (user_id(tweet), user_name(tweet).lower(),
            string(legacy.get('created_at')), string(legacy.get('full_text')),
            string(note.get('text')),
            string(legacy.get('conversation_id_str')),
            string(legacy.get('in_reply_to_status_id_str')),
            string(legacy.get('in_reply_to_user_id_str')),
            string(legacy.get('quoted_status_id_str')),
            tuple(string(obj(m).get('media_key')) for m in media))


def reply_integrity_issue(tweet: dict, all_posts: dict[str, dict]) -> str:
    """Check known graph edges; missing parents are handled as incomplete, not invented."""
    tid = tweet_id(tweet)
    legacy = obj(tweet.get('legacy'))
    parent_raw = string(legacy.get('in_reply_to_status_id_str')).strip()
    parent_id = valid_id(parent_raw)
    conv_raw = string(legacy.get('conversation_id_str')).strip()
    conv_id = valid_id(conv_raw)
    if parent_raw and not parent_id:
        return 'malformed in_reply_to_status_id_str'
    if conv_raw and not conv_id:
        return 'malformed conversation_id_str'
    if parent_id and not conv_id:
        return 'reply has no valid conversation_id_str'
    if not parent_id and conv_id and conv_id != tid:
        return 'non-root conversation_id but missing parent ID'
    if parent_id == tid:
        return 'self-referential reply parent'
    if parent_id and conv_id == tid:
        return 'reply points to itself as conversation root'
    parent_author_raw = string(legacy.get('in_reply_to_user_id_str')).strip()
    parent_author_id = valid_id(parent_author_raw)
    if parent_author_raw and not parent_author_id:
        return 'malformed in_reply_to_user_id_str'
    seen = {tid}
    current = tweet
    for _ in range(256):
        l = obj(current.get('legacy'))
        parent_id = valid_id(l.get('in_reply_to_status_id_str'))
        if not parent_id:
            return ''
        if parent_id in seen:
            return 'cycle in captured reply ancestry'
        seen.add(parent_id)
        parent = all_posts.get(parent_id)
        if parent is None:
            return ''  # Uncaptured ancestor is not proof of a bad relationship.
        parent_legacy = obj(parent.get('legacy'))
        expected_author = valid_id(l.get('in_reply_to_user_id_str'))
        if expected_author and expected_author != user_id(parent):
            return f'parent author ID mismatch for {parent_id}'
        child_conv = valid_id(l.get('conversation_id_str'))
        parent_conv = valid_id(parent_legacy.get('conversation_id_str'))
        if child_conv and parent_conv and child_conv != parent_conv:
            return f'conversation ID mismatch with parent {parent_id}'
        current = parent
    return 'reply ancestry exceeds safe depth (256)'


def available_reply_ancestors(tweet: dict, all_posts: dict[str, dict]) -> tuple[int, str]:
    """Return number of captured ancestor records and first uncaptured parent ID."""
    found = 0
    current = tweet
    seen = {tweet_id(tweet)}
    for _ in range(256):
        parent_id = valid_id(obj(current.get('legacy')).get('in_reply_to_status_id_str'))
        if not parent_id or parent_id in seen:
            return found, ''
        parent = all_posts.get(parent_id)
        if not parent:
            return found, parent_id
        found += 1
        seen.add(parent_id)
        current = parent
    return found, ''


def iter_timeline_results(node: Any):
    """Traverse TimelineTimelineItem and TimelineTimelineModule items, not quoted objects."""
    if isinstance(node, list):
        for sub in node:
            yield from iter_timeline_results(sub)
    elif isinstance(node, dict):
        tweet_results = obj(node.get('tweet_results'))
        if tweet_results:
            candidate = obj(tweet_results.get('result'))
            if tweet_id(candidate) and obj(candidate.get('legacy')):
                yield candidate
            # Tweet wrapper is a leaf: quoted_status_result is context only.
            return
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from iter_timeline_results(value)


def choose_conversation_posts(
    mains: dict[str, dict], target: str, confirmed_user_id: str,
    local_roots: dict[str, str] | None = None,
) -> tuple[dict[str, dict], dict[str, str], Counter]:
    """Mirror precision mode 2's independent descendant snapshots, offline.

    Starting points are target-authored posts in captured timeline entries.
    Follow *actual* reply-parent IDs to reach every loaded descendant, including
    replies by other authors. In a conversation whose target-authored root is
    present, a missing intermediate parent does not hide later replies: accept
    the tweet's explicit conversation root while retaining its missing parent ID.

    A validated local root may be the starting point when the root has been
    deleted from X and only other authors' replies appear in the Responses.
    A reference or @mention of the target elsewhere is never sufficient.
    Quoted status cards are not independent conversation entries.
    """
    seeds = {
        tid for tid, tweet in mains.items()
        if user_name(tweet).casefold() == target.casefold()
        and user_id(tweet) == confirmed_user_id
    }
    roots = {
        tid for tid in seeds
        if not valid_id(obj(mains[tid].get('legacy')).get('in_reply_to_status_id_str'))
        and valid_id(obj(mains[tid].get('legacy')).get('conversation_id_str')) == tid
    }
    # Only roots with a verified local primary snapshot are trusted. These are
    # *not* fabricated response tweets and remain untouched on disk.
    roots.update(local_roots or {})
    children: dict[str, list[str]] = defaultdict(list)
    for tid, tweet in mains.items():
        parent_id = valid_id(obj(tweet.get('legacy')).get('in_reply_to_status_id_str'))
        if parent_id:
            children[parent_id].append(tid)

    selected: dict[str, dict] = {tid: mains[tid] for tid in seeds}
    roles: dict[str, str] = {tid: 'target_author' for tid in seeds}
    # Local roots can be absent from the Responses. Their ID itself is the
    # conversation ID; start a traversal from them without inventing a node.
    todo = deque(sorted(seeds | set(local_roots or {}), key=int))
    while todo:
        parent_id = todo.popleft()
        parent = mains.get(parent_id)
        parent_conversation = (valid_id(obj(parent.get('legacy')).get('conversation_id_str'))
                               if parent is not None else parent_id if parent_id in (local_roots or {}) else '')
        for tid in children.get(parent_id, []):
            if tid in selected:
                continue
            child = mains[tid]
            child_conversation = valid_id(obj(child.get('legacy')).get('conversation_id_str'))
            # Do not select across contradictory or unknown conversation roots.
            if not parent_conversation or child_conversation != parent_conversation:
                continue
            selected[tid] = child
            roles[tid] = 'descendant_reply'
            todo.append(tid)

    # A cursor/pagination gap may omit intermediate replies. For known roots,
    # conversation_id ties the child to a target-authored conversation, even if
    # the direct parent was not present. Never invent that missing parent.
    for tid, tweet in mains.items():
        if tid in selected:
            continue
        legacy = obj(tweet.get('legacy'))
        parent_id = valid_id(legacy.get('in_reply_to_status_id_str'))
        conversation_id = valid_id(legacy.get('conversation_id_str'))
        if parent_id and conversation_id in roots:
            selected[tid] = tweet
            roles[tid] = ('local_root_reply' if conversation_id in (local_roots or {})
                          else 'confirmed_root_reply')

    counts = Counter(roles.values())
    counts['unrelated_timeline_posts'] = len(mains) - len(selected)
    return selected, roles, counts


def collect_response_files(paths: list[str]) -> list[Path]:
    found = []
    seen = set()
    for arg in paths:
        candidate = Path(arg).expanduser()
        if candidate.is_dir():
            files = sorted(p for p in candidate.rglob('*') if p.is_file() and p.suffix.lower() in FILE_SUFFIXES)
        elif candidate.is_file():
            files = [candidate]
        else:
            raise ImportErrorSafe(f'Input not found: {candidate}')
        for p in files:
            resolved = p.resolve()
            if resolved not in seen:
                found.append(resolved)
                seen.add(resolved)
    if not found:
        raise ImportErrorSafe('No .txt / .json response files found.')
    return found


def read_responses(paths: list[Path]) -> tuple[dict, dict, dict, list[str], list[str], Counter]:
    """Return timeline tweets, quotes, source paths, warnings, errors, stats."""
    primary: dict[str, dict] = {}
    quotes: dict[str, dict] = {}
    conflicted: set[str] = set()
    warnings: list[str] = []
    errors: list[str] = []
    sources: dict[str, set[str]] = defaultdict(set)
    stats: Counter = Counter()
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (UnicodeError, ValueError) as exc:
            errors.append({'kind': 'input', 'sources': [str(path)], 'reason': '不是有效 JSON，已跳过'})
            continue
        if not isinstance(data, dict):
            errors.append({'kind': 'input', 'sources': [str(path)], 'reason': 'JSON 根结构异常，已跳过'})
            continue
        # Accept captured Twitter/X timeline responses. No arbitrary API records.
        roots = [obj(data.get('data'))]
        if not any('threaded_conversation_with_injections' in k or k.startswith('user_result') or k.startswith('user_tweets') for k in roots[0]):
            warnings.append({'kind': 'input', 'sources': [str(path)], 'reason': '响应格式不常见，已尝试识别推文'})
        count_here = 0
        for tweet in iter_timeline_results(data):
            tid = tweet_id(tweet)
            issue = author_integrity_issue(tweet)
            if not tid or issue:
                stats['invalid_author'] += 1
                errors.append({'kind': 'input', 'sources': [str(path)], 'reason': '推文 ' + (tid or '未知ID') + ' 校验失败，已跳过'})
                continue
            count_here += 1
            sources[tid].add(str(path))
            if tid in conflicted:
                continue
            old = primary.get(tid)
            if old is not None:
                if tweet_signature(old) != tweet_signature(tweet):
                    errors.append({'kind': 'input', 'sources': sorted(sources.get(tid, [])), 'reason': '推文 ' + tid + ' 在多份响应中关系冲突，未导入'})
                    conflicted.add(tid)
                    del primary[tid]
                continue
            primary[tid] = tweet
        stats['timeline_items'] += count_here
        stats['files_parsed'] += 1
    # Quoted nested posts may be absent from the timeline and must not be main items.
    def get_quote(tweet: dict, depth: int = 0):
        if depth > 8:
            return
        result = obj(obj(tweet.get('quoted_status_result')).get('result'))
        tid = tweet_id(result)
        if tid and obj(result.get('legacy')) and not author_integrity_issue(result):
            quotes.setdefault(tid, result)
            get_quote(result, depth + 1)
    for main in primary.values():
        get_quote(main)
    stats['unique_timeline_tweets'] = len(primary)
    stats['quoted_context'] = len(quotes)
    return primary, quotes, sources, warnings, errors, stats


def utf16_offset_to_index(text: str, offset: int) -> int:
    """GraphQL legacy indices count UTF-16 units; X API v2 uses Unicode positions."""
    used = 0
    for pos, ch in enumerate(text):
        if used >= offset:
            return pos
        used += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def entity_list(legacy: dict) -> dict:
    result: dict = {}
    source = obj(legacy.get('entities'))
    mapping = {'urls': 'urls', 'user_mentions': 'mentions', 'hashtags': 'hashtags', 'symbols': 'cashtags'}
    for old_key, new_key in mapping.items():
        converted = []
        for item in sequence(source.get(old_key)):
            item = obj(item)
            if not item:
                continue
            dest = {k: v for k, v in item.items() if k not in ('indices', 'id_str', 'screen_name')}
            indices = sequence(item.get('indices'))
            if len(indices) == 2 and all(isinstance(n, int) for n in indices):
                dest['start'], dest['end'] = (utf16_offset_to_index(string(legacy.get('full_text')), indices[0]),
                                             utf16_offset_to_index(string(legacy.get('full_text')), indices[1]))
            if old_key == 'user_mentions':
                dest['id'] = string(item.get('id_str'))
                dest['username'] = string(item.get('screen_name'))
            converted.append(dest)
        if converted:
            result[new_key] = converted
    return result


def convert_user(t: dict) -> dict:
    u = user_result(t)
    core = obj(u.get('core'))
    counts = obj(u.get('relationship_counts'))
    bio = obj(u.get('profile_bio'))
    loc = obj(u.get('location'))
    profile = {
        'id': user_id(t),
        'username': string(core.get('screen_name')),
        'name': string(core.get('name')),
        'description': string(bio.get('description')),
        'profile_image_url': string(obj(u.get('avatar')).get('image_url')),
        'protected': bool(obj(u.get('privacy')).get('protected', False)),
        'verified': bool(obj(u.get('verification')).get('verified', False)),
        'public_metrics': {
            'followers_count': counts.get('followers', 0),
            'following_count': counts.get('following', 0),
            'tweet_count': obj(u.get('tweet_counts')).get('tweets', 0),
        },
    }
    location = string(loc.get('location')).strip()
    if location:
        profile['location'] = location
    return profile


def convert_media(m: dict) -> dict | None:
    key = string(m.get('media_key')).strip()
    kind = string(m.get('type')).strip()
    if not key or kind not in ('photo', 'video', 'animated_gif'):
        return None
    result: dict = {'media_key': key, 'type': kind}
    url = string(m.get('media_url_https') or m.get('media_url'))
    if kind == 'photo':
        if url:
            result['url'] = url
    elif url:
        result['preview_image_url'] = url
    orig = obj(m.get('original_info'))
    if isinstance(orig.get('width'), int):
        result['width'] = orig['width']
    if isinstance(orig.get('height'), int):
        result['height'] = orig['height']
    vinfo = obj(m.get('video_info'))
    variants = []
    for v in sequence(vinfo.get('variants')):
        v = obj(v)
        target = {'url': string(v.get('url')), 'content_type': string(v.get('content_type'))}
        if not target['url'] or not target['content_type']:
            continue
        bitrate = v.get('bit_rate', v.get('bitrate'))
        if isinstance(bitrate, int):
            target['bit_rate'] = bitrate  # WTD selects MP4 by bit_rate, NOT bitrate.
        variants.append(target)
    if variants:
        result['variants'] = variants
    if isinstance(vinfo.get('duration_millis'), int):
        result['duration_ms'] = vinfo['duration_millis']
    return result


def convert_tweet(tweet: dict) -> tuple[dict, list[dict]]:
    legacy = obj(tweet.get('legacy'))
    tid = tweet_id(tweet)
    created = iso_date(legacy.get('created_at'))
    if not (tid and user_id(tweet) and created):
        raise ImportErrorSafe(f'Incomplete id/author/date for tweet {tid or "unknown"}')
    note = obj(obj(obj(tweet.get('note_tweet')).get('note_tweet_results')).get('result'))
    note_text = string(note.get('text'))
    text = note_text or string(legacy.get('full_text'))
    out = {
        'id': tid,
        'text': text,
        'created_at': created,
        'author_id': user_id(tweet),
        'conversation_id': valid_id(legacy.get('conversation_id_str')) or tid,
        'lang': string(legacy.get('lang')),
        'public_metrics': {
            'retweet_count': legacy.get('retweet_count', 0),
            'reply_count': legacy.get('reply_count', 0),
            'like_count': legacy.get('favorite_count', 0),
            'quote_count': legacy.get('quote_count', 0),
            'bookmark_count': legacy.get('bookmark_count', 0),
        },
    }
    if 'possibly_sensitive' in legacy:
        out['possibly_sensitive'] = bool(legacy['possibly_sensitive'])
    if note_text:
        out['note_tweet'] = {'text': note_text}
    entities = entity_list(legacy)
    if entities:
        out['entities'] = entities
    parent = valid_id(legacy.get('in_reply_to_status_id_str'))
    quoted = valid_id(legacy.get('quoted_status_id_str'))
    refs = []
    if parent:
        refs.append({'type': 'replied_to', 'id': parent})
        parent_author = valid_id(legacy.get('in_reply_to_user_id_str'))
        if parent_author:
            out['in_reply_to_user_id'] = parent_author
    if quoted:
        refs.append({'type': 'quoted', 'id': quoted})
    if refs:
        out['referenced_tweets'] = refs
    edits = obj(tweet.get('edit_control'))
    if edits:
        out['edit_controls'] = deepcopy(edits)
    media = []
    keys = []
    seen = set()
    source = sequence(obj(legacy.get('extended_entities')).get('media')) or sequence(obj(legacy.get('entities')).get('media'))
    for raw in source:
        entry = convert_media(obj(raw))
        if entry and entry['media_key'] not in seen:
            media.append(entry)
            keys.append(entry['media_key'])
            seen.add(entry['media_key'])
    if keys:
        out['attachments'] = {'media_keys': keys}
    return out, media


def build_payload(main: dict, all_posts: dict[str, dict], core=None, archive_user: str = '') -> dict:
    main_id = tweet_id(main)
    related: dict[str, dict] = {main_id: main}
    # Only quote cards and direct reply ancestry; not unrelated timeline siblings.
    def add_quotes(t: dict, depth=0):
        if depth > 8:
            return
        q = obj(obj(t.get('quoted_status_result')).get('result'))
        qid = tweet_id(q)
        if qid and qid not in related:
            related[qid] = q
            add_quotes(q, depth+1)
    add_quotes(main)
    ancestor_ids = []
    seen = {main_id}
    current = main
    while True:
        parent_id = valid_id(obj(current.get('legacy')).get('in_reply_to_status_id_str'))
        if not parent_id or parent_id in seen or len(ancestor_ids) >= 32:
            break
        seen.add(parent_id)
        parent = all_posts.get(parent_id)
        if not parent:
            break
        related[parent_id] = parent
        ancestor_ids.append(parent_id)
        add_quotes(parent)
        current = parent
    converted: dict[str, dict] = {}
    media_by_key: dict[str, dict] = {}
    users_by_id: dict[str, dict] = {}
    for tid, t in related.items():
        try:
            converted[tid], media = convert_tweet(t)
        except ImportErrorSafe:
            if tid == main_id:
                raise
            continue
        users_by_id[user_id(t)] = convert_user(t)
        for m in media:
            media_by_key.setdefault(m['media_key'], m)
    # Also preserve profiles for mentioned users when supplied in the response.
    payload = {
        'data': converted[main_id],
        'includes': {
            'users': list(users_by_id.values()),
            'tweets': [t for tid, t in converted.items() if tid != main_id],
            'media': list(media_by_key.values()),
        },
    }
    if core is not None:
        cache = core._get_x_api_json_cache(payload, create=True)
        cache.update({'source': 'browser_graphql_response', 'archive_user': archive_user or user_name(main),
                      'fetched_at': core._utc_now_iso()})
        # Parent beyond direct parent: use WTD's existing reply-chain cache format.
        if len(ancestor_ids) >= 2:
            grandparent = ancestor_ids[1]
            upstream = []
            for tid in ancestor_ids[1:]:
                if tid in converted:
                    post = {'data': converted[tid], 'includes': {
                        'users': list(users_by_id.values()),
                        'media': list(media_by_key.values()),
                    }}
                    upstream.append((tid, post, 'browser_response'))
            if upstream:
                core._store_x_api_reply_chain(payload, grandparent, len(upstream), upstream)
    return payload



def _reply_parent_id(node: dict) -> str:
    for ref in sequence(node.get('referenced_tweets')):
        if obj(ref).get('type') == 'replied_to':
            return valid_id(obj(ref).get('id'))
    return valid_id(obj(node.get('legacy')).get('in_reply_to_status_id_str'))


def _v2_context_from_payload(payload: dict, required_id: str) -> tuple[dict, list, list] | None:
    """Extract an exact-ID v2 post and its genuine author/media context."""
    data = obj(payload.get('data'))
    candidates = ([data] if data else []) + sequence(obj(payload.get('includes')).get('tweets'))
    for item in candidates:
        node = obj(item)
        if valid_id(node.get('id')) != required_id:
            continue
        uid = valid_id(node.get('author_id'))
        if not uid or not string(node.get('created_at')).strip():
            continue
        if not string(node.get('text') or obj(node.get('note_tweet')).get('text')).strip() and not sequence(obj(node.get('attachments')).get('media_keys')):
            continue
        users = [obj(u) for u in sequence(obj(payload.get('includes')).get('users'))
                 if valid_id(obj(u).get('id')) == uid and string(obj(u).get('username')).strip()]
        if not users:
            continue
        keys = set(string(k) for k in sequence(obj(node.get('attachments')).get('media_keys')))
        media = [obj(m) for m in sequence(obj(payload.get('includes')).get('media'))
                 if string(obj(m).get('media_key')) in keys]
        return deepcopy(node), deepcopy(users), deepcopy(media)
    return None


def _context_from_payload(core, payload: dict, tid: str, embedded: bool = True):
    """Accept matching v2 tweets; also support native GraphQL tweet nodes."""
    found = _v2_context_from_payload(payload, tid)
    if found:
        # Primary-only lookups must not resolve unrelated nested references.
        if embedded or valid_id(obj(payload.get('data')).get('id')) == tid:
            return found
    if not embedded:
        node = obj(payload.get('data'))
        if tweet_id(node) != tid:
            return None
    else:
        node = {}
        for candidate in core._iter_candidate_tweet_nodes(payload):
            if tweet_id(obj(candidate)) == tid:
                node = obj(candidate)
                break
    if tweet_id(node) == tid and obj(node.get('legacy')) and not author_integrity_issue(node):
        try:
            converted, media = convert_tweet(node)
            return converted, [convert_user(node)], media
        except (ImportErrorSafe, TypeError, ValueError):
            return None
    return None


class LocalContextResolver:
    """Read local context by exact tweet ID, never guessing from a filename.

    Existing primary JSON is preferred. As a last resort, scan nested includes.tweets
    (e.g. a root preserved inside a reply payload without a standalone snapshot).
    No source JSON is ever modified by this resolver.
    """
    def __init__(self, core, json_dir: Path, existing: dict[str, Path]):
        self.core, self.json_dir, self.existing = core, json_dir, existing
        self.by_id: dict[str, tuple | None] = {}
        self.embedded_paths: dict[str, Path] = {}
        self.embedded_cache_paths: dict[str, tuple[Path, str, int]] = {}
        self.embedded_scanned = False

    def _load(self, path: Path, tid: str, embedded: bool):
        if not path.is_file():
            return None
        data = self.core.load_snapshot_json(path)
        if not isinstance(data, dict):
            return None
        return _context_from_payload(self.core, data, tid, embedded=embedded)

    def _scan_embedded(self):
        if self.embedded_scanned:
            return
        self.embedded_scanned = True
        for path in sorted(self.json_dir.glob('*.json')):
            data = self.core.load_snapshot_json(path)
            if not isinstance(data, dict):
                continue
            for node in sequence(obj(data.get('includes')).get('tweets')):
                nid = valid_id(obj(node).get('id'))
                if nid and nid not in self.embedded_paths:
                    self.embedded_paths[nid] = path
            chains = obj(obj(data.get('_pale_fire')).get('x_api_reply_chains'))
            for key, entry in chains.items():
                for index, post in enumerate(sequence(obj(entry).get('posts'))):
                    tid = valid_id(obj(post).get('id'))
                    if tid and tid not in self.embedded_cache_paths:
                        self.embedded_cache_paths[tid] = (path, key, index)

    def get(self, tid: str):
        if not tid:
            return None
        if tid in self.by_id:
            return self.by_id[tid]
        paths = []
        path = self.existing.get(tid)
        if path:
            paths.append(path)
        paths.extend(sorted(self.json_dir.glob(f'*_{tid}.json')))
        for path in dict.fromkeys(paths):
            result = self._load(path, tid, embedded=False)
            if result:
                self.by_id[tid] = result
                return result
        self._scan_embedded()
        path = self.embedded_paths.get(tid)
        if path:
            result = self._load(path, tid, embedded=True)
            if result:
                self.by_id[tid] = result
                return result
        cache_location = self.embedded_cache_paths.get(tid)
        if cache_location:
            path, key, index = cache_location
            container = self.core.load_snapshot_json(path)
            chains = obj(obj(obj(container).get('_pale_fire')).get('x_api_reply_chains'))
            posts = sequence(obj(chains.get(key)).get('posts'))
            if index < len(posts) and valid_id(obj(posts[index]).get('id')) == tid:
                result = _context_from_payload(
                    self.core, obj(obj(posts[index]).get('payload')), tid, embedded=True)
                if result:
                    self.by_id[tid] = result
                    return result
        self.by_id[tid] = None
        return None


def enrich_reply_context(payload: dict, graph_posts: dict[str, dict],
                         resolver: LocalContextResolver, core) -> tuple[int, str]:
    """Embed the verifiable, consecutive direct-parent-to-root ancestry.

    Only includes and the importer's own chain cache are enriched; primary data
    (text, author, media, IDs) is never modified. No fabricated ancestors.
    """
    primary = obj(payload.get('data'))
    main_id = valid_id(primary.get('id'))
    if not main_id:
        return 0, ''
    includes = payload.setdefault('includes', {})
    if not isinstance(includes, dict):
        return 0, ''
    tweets = includes.setdefault('tweets', [])
    users = includes.setdefault('users', [])
    media = includes.setdefault('media', [])
    if not all(isinstance(x, list) for x in (tweets, users, media)):
        return 0, ''
    included = {valid_id(obj(n).get('id')): n for n in tweets if valid_id(obj(n).get('id'))}
    user_ids = {valid_id(obj(u).get('id')) for u in users}
    media_keys = {string(obj(m).get('media_key')) for m in media}
    chain: list[dict] = []
    current = primary
    seen = {main_id}
    newly_added = 0
    gap = ''
    for _ in range(32):
        parent_id = _reply_parent_id(current)
        if not parent_id:
            break
        if parent_id in seen:
            gap = parent_id
            break
        seen.add(parent_id)
        parent = included.get(parent_id)
        people = []
        attachments = []
        if parent is None:
            captured = graph_posts.get(parent_id)
            if captured and not author_integrity_issue(captured):
                try:
                    parent, attachments = convert_tweet(captured)
                    people = [convert_user(captured)]
                except (ImportErrorSafe, TypeError, ValueError):
                    parent = None
            if parent is None:
                local = resolver.get(parent_id)
                if local:
                    parent, people, attachments = local
            if parent is None:
                gap = parent_id
                break
        if valid_id(parent.get('id')) != parent_id:
            gap = parent_id
            break
        expected_user = valid_id(current.get('in_reply_to_user_id'))
        actual_user = valid_id(parent.get('author_id'))
        if expected_user and expected_user != actual_user:
            gap = parent_id
            break
        current_conv = valid_id(current.get('conversation_id'))
        parent_conv = valid_id(parent.get('conversation_id'))
        if current_conv and parent_conv and current_conv != parent_conv:
            gap = parent_id
            break
        if parent_id not in included:
            tweets.append(deepcopy(parent))
            included[parent_id] = parent
            newly_added += 1
        for u in people:
            uid = valid_id(obj(u).get('id'))
            if uid and uid not in user_ids:
                users.append(deepcopy(u))
                user_ids.add(uid)
        for m in attachments:
            mid = string(obj(m).get('media_key'))
            if mid and mid not in media_keys:
                media.append(deepcopy(m))
                media_keys.add(mid)
        chain.append(parent)
        current = parent
    # WTD reads the direct parent from includes.tweets. To render deeper
    # ancestry at depth=0 it reads the cached chain keyed by grandparent.
    if len(chain) >= 2:
        grandparent_id = valid_id(chain[1].get('id'))
        cache = obj(obj(payload.get('_pale_fire')).get('x_api_reply_chains'))
        existing = obj(cache.get(grandparent_id))
        known = {string(obj(x).get('id')) for x in sequence(existing.get('posts'))}
        desired = [valid_id(p.get('id')) for p in chain[1:]]
        cached_error_id = valid_id(obj(existing.get('terminal_error')).get('id'))
        if (not existing or not set(desired).issubset(known)
                or cached_error_id in set(desired)):
            upstream = [(valid_id(p.get('id')),
                         {'data': deepcopy(p), 'includes': {'users': deepcopy(users), 'media': deepcopy(media)}},
                         'browser_response_or_local') for p in chain[1:]]
            core._store_x_api_reply_chain(payload, grandparent_id, len(upstream), upstream)
            newly_added += 1  # cache extension counts as a context change
    return newly_added, gap


def _write_enriched_existing_json(core, path: Path, old: dict, enriched: dict, tid: str):
    """Persist verified context-only improvement atomically, no .bak files."""
    if obj(old.get('data')) != obj(enriched.get('data')):
        raise ImportErrorSafe(f'推文 {tid} 的正文发生变化，已拒绝修复')
    primary = obj(enriched.get('data'))
    # Raw Wayback/GraphQL payloads may have a non-tweet `data.id` wrapper.
    if valid_id(primary.get('author_id')) and valid_id(primary.get('id')) != tid:
        raise ImportErrorSafe(f'推文 {tid} 的主推文 ID 不匹配，已拒绝修复')
    tmp = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8',
                                         prefix='.context-', suffix='.tmp',
                                         delete=False) as fh:
            tmp = Path(fh.name)
            json.dump(enriched, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        check = core.load_snapshot_json(tmp)
        if not isinstance(check, dict) or check != enriched:
            raise ImportErrorSafe(f'推文 {tid} 的临时 JSON 校验失败，原文件未动')
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


# --- v16: automatically refresh previously archived child replies after a new parent import ---

def _primary_reply_identity(core, payload: dict, path: Path) -> tuple[str, str, str, str] | None:
    """Identify a real on-disk PRIMARY tweet (not a tweet merely embedded in its file).

    Return (id, parent_id, snapshot_timestamp, original_url). Ignore malformed and
    ambiguous files instead of altering unrelated historic snapshots.
    """
    parsed = core.parse_snapshot_filename(path.name)
    if not parsed:
        return None
    ts, expected_id = parsed
    if not valid_id(expected_id):
        return None
    url = core.reconstruct_original_url('i', expected_id)
    node = core.find_primary_tweet_node(payload, url)
    if not isinstance(node, dict):
        return None
    meta = core._extract_metadata_from_node(node)
    tid = valid_id(meta.get('tweet_id'))
    if tid != expected_id:
        return None
    parent_id = valid_id(meta.get('in_reply_to_status_id'))
    if not parent_id:
        # Different WTD snapshot formats have different metadata extractors.
        for ref in sequence(node.get('referenced_tweets')):
            if obj(ref).get('type') == 'replied_to':
                parent_id = valid_id(obj(ref).get('id'))
                break
        if not parent_id:
            parent_id = valid_id(obj(node.get('legacy')).get('in_reply_to_status_id_str'))
    return tid, parent_id, ts, url


def _embedded_reply_parent_ids(core, payload: dict):
    """Yield verified (tweet_id, parent_id) edges from includes and cached posts.

    Cache entries are only inspected as context. They are never counted as
    independent snapshots, and are never themselves written or regenerated.
    """
    includes = obj(payload.get('includes'))
    for raw in sequence(includes.get('tweets')):
        node = obj(raw)
        tid = valid_id(node.get('id')) or tweet_id(node)
        parent = _reply_parent_id(node)
        if tid and parent and tid != parent:
            yield tid, parent
    caches = obj(obj(payload.get('_pale_fire')).get('x_api_reply_chains'))
    for entry in caches.values():
        for post in sequence(obj(entry).get('posts')):
            item = obj(post)
            tid = valid_id(item.get('id'))
            post_payload = obj(item.get('payload'))
            if not tid or not post_payload:
                continue
            # Never infer an edge from a cache key alone; require matching real data.
            primary = core.find_primary_tweet_node(
                post_payload, core.reconstruct_original_url('i', tid))
            if not isinstance(primary, dict):
                continue
            meta = core._extract_metadata_from_node(primary)
            if valid_id(meta.get('tweet_id')) != tid:
                continue
            parent = valid_id(meta.get('in_reply_to_status_id')) or _reply_parent_id(primary)
            if parent and parent != tid:
                yield tid, parent


def find_affected_existing_descendants(core, json_dir: Path, imported_ids: set[str],
                                       imported_paths: set[Path]) -> list[tuple[Path, str, str]]:
    """Find descendants via primary tweet IDs and verified embedded intermediate links.

    A new root A -> embedded-only intermediate B -> existing child C is included
    even when B has no standalone JSON. Only C's real primary snapshot is edited.
    """
    if not imported_ids:
        return []
    primary_by_id: dict[str, list[tuple[Path, str, str]]] = defaultdict(list)
    edge_claims: dict[str, set[str]] = defaultdict(set)
    for path in sorted(json_dir.glob('*.json')):
        if path in imported_paths:
            continue
        payload = core.load_snapshot_json(path)
        if not isinstance(payload, dict):
            continue
        identity = _primary_reply_identity(core, payload, path)
        if not identity:
            continue
        tid, parent_id, ts, url = identity
        primary_by_id[tid].append((path, tid, url))
        if parent_id and parent_id != tid:
            edge_claims[tid].add(parent_id)
        for child_id, ancestor_id in _embedded_reply_parent_ids(core, payload):
            edge_claims[child_id].add(ancestor_id)

    # Reject contradictory parent edges for the SAME tweet ID, rather than
    # making an ambiguous cross-conversation association.
    children: dict[str, set[str]] = defaultdict(set)
    for tid, parents in edge_claims.items():
        if len(parents) == 1:
            children[next(iter(parents))].add(tid)
    pending = deque(sorted(imported_ids, key=int))
    reachable = set(imported_ids)
    while pending:
        ancestor = pending.popleft()
        for child in sorted(children.get(ancestor, ()), key=int):
            if child not in reachable:
                reachable.add(child)
                pending.append(child)
    result = []
    for tid in sorted(reachable - imported_ids, key=int):
        result.extend(primary_by_id.get(tid, []))
    return sorted(result, key=lambda item: item[0])


def _verified_v2_ancestor_ids(payload: dict) -> set[str]:
    """Return only consecutive, identity-consistent ancestors of the main tweet."""
    primary = obj(payload.get('data'))
    included = {valid_id(obj(x).get('id')): obj(x)
                for x in sequence(obj(payload.get('includes')).get('tweets'))
                if valid_id(obj(x).get('id'))}
    seen = {valid_id(primary.get('id'))}
    confirmed = set()
    current = primary
    for _ in range(32):
        parent_id = _reply_parent_id(current)
        if not parent_id or parent_id in seen:
            break
        parent = included.get(parent_id)
        if not parent:
            break
        claimed_author = valid_id(current.get('in_reply_to_user_id'))
        if claimed_author and claimed_author != valid_id(parent.get('author_id')):
            break
        claimed_conv = valid_id(current.get('conversation_id'))
        actual_conv = valid_id(parent.get('conversation_id'))
        if claimed_conv and actual_conv and claimed_conv != actual_conv:
            break
        confirmed.add(parent_id)
        seen.add(parent_id)
        current = parent
    return confirmed


def _clear_resolved_cached_errors(payload: dict, verified_ids: set[str]) -> int:
    """Remove ONLY obsolete terminal errors whose missing tweet has just been verified.

    Never erase an error for an ancestor still absent from the real local data.
    """
    if not verified_ids:
        return 0
    pf = obj(payload.get('_pale_fire'))
    chains = obj(pf.get('x_api_reply_chains'))
    removed = 0
    for chain_id, entry in list(chains.items()):
        if not isinstance(entry, dict):
            continue
        error = obj(entry.get('terminal_error'))
        error_id = valid_id(error.get('id'))
        if not error_id or error_id not in verified_ids:
            continue
        # An empty failed lookup can be dropped; a populated chain retains posts.
        if not sequence(entry.get('posts')):
            del chains[chain_id]
        else:
            entry.pop('terminal_error', None)
            recovery = obj(entry.get('recovery'))
            if valid_id(recovery.get('missing_id')) == error_id:
                entry.pop('recovery', None)
        removed += 1
    return removed


def _strip_nested_cache_from_new_chain_posts(before: dict, after: dict) -> None:
    """Prevent WTD's local-only repair from recursively copying _pale_fire caches.

    Keep all pre-existing cache entries intact; strip _pale_fire ONLY from
    newly generated/replaced embedded payloads. The tweet data and includes
    remain present, so offline reply-chain rendering still works.
    """
    old_chains = obj(obj(before.get('_pale_fire')).get('x_api_reply_chains'))
    new_chains = obj(obj(after.get('_pale_fire')).get('x_api_reply_chains'))
    for key, entry in new_chains.items():
        if not isinstance(entry, dict) or entry == old_chains.get(key):
            continue
        for post in sequence(entry.get('posts')):
            embedded = obj(obj(post).get('payload'))
            embedded.pop('_pale_fire', None)


def auto_repair_existing_descendants(core, json_dir: Path, existing: dict[str, Path],
                                     newly_written: list, graph_posts: dict[str, dict]) -> tuple[int, int, int]:
    """Run automatically after stage 1, before HTML generation, using ONLY local JSON.

    V2 snapshots get embedded includes.tweets + a lean WTD cache. Other WTD
    formats receive their native local-only no-resume cache refresh. Primary
    data is never modified; each candidate is checked again before atomic write.
    """
    imported_ids: set[str] = set()
    imported_paths: set[Path] = set()
    complete_existing = dict(existing)
    for record in newly_written:
        path = record.json_path
        payload = core.load_snapshot_json(path)
        identity = _primary_reply_identity(core, payload, path) if isinstance(payload, dict) else None
        if identity:
            imported_ids.add(identity[0])
            imported_paths.add(path)
            complete_existing[identity[0]] = path
    if not imported_ids:
        return 0, 0, 0

    affected = find_affected_existing_descendants(core, json_dir, imported_ids, imported_paths)
    if not affected:
        return 0, 0, 0

    # WTD also caches *in-memory* indexes; invalidate any built before stage 1.
    for attr in ('LOCAL_TWEET_INDEX_CACHE', 'LOCAL_CONVERSATION_INDEX_CACHE',
                 '_WTD_EMBEDDED_REPLY_FALLBACK_V3_INDEX'):
        cache = getattr(core, attr, None)
        if isinstance(cache, dict):
            cache.pop(str(json_dir.resolve()), None)
    resolver = LocalContextResolver(core, json_dir, complete_existing)
    repaired = 0
    failures = 0
    for path, tid, url in affected:
        try:
            original = core.load_snapshot_json(path)
            if not isinstance(original, dict):
                failures += 1
                continue
            identity = _primary_reply_identity(core, original, path)
            if not identity or identity[0] != tid or not identity[1]:
                failures += 1
                continue
            enriched = deepcopy(original)
            primary = obj(enriched.get('data'))
            is_v2 = valid_id(primary.get('id')) == tid and valid_id(primary.get('author_id'))
            if is_v2:
                # Importer's existing validated direct-parent -> root enrichment.
                enrich_reply_context(enriched, graph_posts, resolver, core)
                # Verified includes plus new primary JSON supersede cached 404s.
                _clear_resolved_cached_errors(
                    enriched, _verified_v2_ancestor_ids(enriched) & imported_ids)
            else:
                # WTD Wayback/legacy snapshots: use its own repair mechanism,
                # explicitly forcing cache bypass and forbidding all network fallback.
                changed = []
                core.extract_tweet_content(
                    enriched, url,
                    x_bearer_token='',
                    x_api_reply_chain_depth=8,
                    x_api_enrichment_changed=changed,
                    x_api_use_json_cache=False,
                    x_api_retry_cached_errors=True,
                    reply_chain_lookup_context=core.ReplyChainLookupContext(
                        json_dir=json_dir, snapshot_timestamp=identity[2],
                        use_local_json=True, use_wayback=False,
                        allow_conversation_wayback=False),
                    allow_reply_chain_media_lookup=False,
                )
                _strip_nested_cache_from_new_chain_posts(original, enriched)
            if original == enriched:
                continue
            # Preserve the whole original primary object, regardless of source.
            if original.get('data') != enriched.get('data'):
                raise ImportErrorSafe(f'推文 {tid} 主体发生变化，拒绝写回')
            # If a file was independently modified during repair, never overwrite it.
            if core.load_snapshot_json(path) != original:
                raise ImportErrorSafe(f'推文 {tid} 文件在修复时发生变化，已跳过')
            _write_enriched_existing_json(core, path, original, enriched, tid)
            repaired += 1
        except Exception as exc:
            failures += 1
            print(f'警告：自动修复已有下级回复 {tid} 失败：{type(exc).__name__}: {exc}')
    return len(affected), repaired, failures


def collect_existing(core, asset_dir: Path, json_dir: Path, target: str):
    existing: dict[str, Path] = {}
    occupied: set[str] = set()
    manifest_path = core.snapshot_manifest_path(asset_dir)
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (UnicodeError, ValueError) as exc:
            raise ImportErrorSafe(f'Existing snapshots.json is unreadable: {exc}') from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get('snapshots'), list):
            raise ImportErrorSafe('Existing snapshots.json structure invalid; refusing changes')
        if manifest.get('user') and string(manifest['user']).lower() != target.lower():
            raise ImportErrorSafe(f'Archive username mismatch: manifest={manifest["user"]} target={target}')
    else:
        manifest = {'user': target, 'snapshots': []}
    # Parse every actual file, not only manifest: avoids accidental overwrites.
    for path in sorted(json_dir.glob('*.json')):
        parsed = core.parse_snapshot_filename(path.name)
        if parsed:
            occupied.add(parsed[1])
        payload = core.load_snapshot_json(path)
        if not isinstance(payload, dict):
            continue
        metadata = core.extract_tweet_metadata(payload, 'https://twitter.com/i/status/' + (parsed[1] if parsed else '0'))
        tid = valid_id(metadata.get('tweet_id'))
        if tid:
            existing.setdefault(tid, path)
            occupied.add(tid)
    for entry in manifest['snapshots']:
        if not isinstance(entry, dict):
            continue
        filename = string(entry.get('json_filename'))
        if not filename:
            continue
        # A stale manifest entry alone is NOT evidence that the tweet is saved.
        # If the referenced JSON has disappeared, allow restoring it from the
        # supplied Response. Existing on-disk files remain protected, including
        # files that cannot be parsed (those need manual inspection).
        if not (json_dir / filename).is_file():
            continue
        match = re.search(r'/status/(\d+)', string(entry.get('original_url')))
        if match:
            occupied.add(match.group(1))
        parsed = core.parse_snapshot_filename(filename)
        if parsed:
            occupied.add(parsed[1])
    return manifest, existing, occupied



def _local_root_author(core, path: Path, root_id: str) -> tuple[str, str, str]:
    """Independently verify a local WTD root snapshot's ID and author identity.

    A manifest entry or original URL is not enough to authorize importing other
    people's replies. Require a real JSON file with an extractable primary post
    and a matching author user profile. Fail closed on incomplete records.
    """
    payload = core.load_snapshot_json(path)
    if not isinstance(payload, dict):
        return '', '', 'unreadable local JSON'
    url = core.reconstruct_original_url('i', root_id)
    node = core.find_primary_tweet_node(payload, url)
    if not isinstance(node, dict):
        return '', '', 'cannot locate primary tweet'
    metadata = core._extract_metadata_from_node(node)
    if valid_id(metadata.get('tweet_id')) != root_id:
        return '', '', 'primary tweet ID does not match filename'
    if valid_id(metadata.get('in_reply_to_status_id')):
        return '', '', 'local snapshot is itself a reply, not a root'
    conv_raw = string(metadata.get('conversation_id')).strip()
    if conv_raw and conv_raw != root_id:
        return '', '', 'local root has a different conversation_id'
    if metadata.get('is_repost') == 'true':
        return '', '', 'local snapshot represents a repost'

    legacy = obj(node.get('legacy'))
    profile = obj(obj(obj(node.get('core')).get('user_results')).get('result'))
    primary_id = valid_id(node.get('author_id') or legacy.get('user_id_str')
                          or profile.get('rest_id') or obj(node.get('user')).get('id_str'))
    if not primary_id:
        return '', '', 'local root has no reliable author ID'
    # Prefer an exact author-id match in v2 includes.users. Do not trust the
    # filename, URL or unrelated users (including mentioned / quoted authors).
    usernames = set()
    for user in sequence(obj(payload.get('includes')).get('users')):
        user = obj(user)
        if valid_id(user.get('id') or user.get('id_str')) == primary_id:
            name = string(user.get('username') or user.get('screen_name')).strip()
            if name:
                usernames.add(name.casefold())
    # Legacy/GraphQL root author: only accept a user embedded in this *root*
    # node, and cross-check its numeric identity where supplied.
    if profile:
        p_id = valid_id(profile.get('rest_id'))
        if p_id and p_id != primary_id:
            return '', '', 'embedded root profile author ID mismatch'
        name = string(obj(profile.get('core')).get('screen_name') or
                      obj(profile.get('legacy')).get('screen_name')).strip()
        if name:
            usernames.add(name.casefold())
    embedded_user = obj(node.get('user'))
    if embedded_user:
        u_id = valid_id(embedded_user.get('id_str') or embedded_user.get('rest_id'))
        if u_id and u_id != primary_id:
            return '', '', 'embedded root author ID mismatch'
        name = string(embedded_user.get('screen_name')).strip()
        if name:
            usernames.add(name.casefold())
    direct_name = string(node.get('screen_name') or legacy.get('screen_name')).strip()
    if direct_name:
        usernames.add(direct_name.casefold())
    if len(usernames) != 1:
        return '', '', 'root author username missing or contradictory'
    return next(iter(usernames)), primary_id, ''


def verify_local_roots(core, mains: dict[str, dict], existing: dict[str, Path],
                       target: str) -> tuple[dict[str, str], dict[str, str]]:
    """Verify on-disk local root content; return verified roots and failures.

    Failure messages are joined to EVERY captured reply in the caller so an
    error is never reported with just an unhelpful root tweet ID.
    """
    root_ids = sorted({
        valid_id(obj(tweet.get('legacy')).get('conversation_id_str'))
        for tweet in mains.values()
        if valid_id(obj(tweet.get('legacy')).get('in_reply_to_status_id_str'))
        and valid_id(obj(tweet.get('legacy')).get('conversation_id_str'))
        and valid_id(obj(tweet.get('legacy')).get('conversation_id_str')) != tweet_id(tweet)
    }, key=int)
    verified: dict[str, str] = {}
    failed: dict[str, str] = {}
    for root_id in root_ids:
        path = existing.get(root_id)
        if not path or not path.is_file():
            failed[root_id] = 'no local WTD root snapshot; cannot verify conversation ownership'
            continue
        author, author_id, issue = _local_root_author(core, path, root_id)
        if issue:
            failed[root_id] = f'local root not usable ({issue})'
            continue
        if author != target.casefold():
            failed[root_id] = f'local root author @{author} differs from target @{target}'
            continue
        captured = mains.get(root_id)
        if captured and (user_id(captured) != author_id or user_name(captured).casefold() != author):
            failed[root_id] = 'local and captured root authors disagree'
            continue
        verified[root_id] = author_id
    if len(set(verified.values())) > 1:
        raise ImportErrorSafe('Verified local roots claim different author IDs for '
                              f'@{target}; refusing ambiguous archive identity')
    return verified, failed


def unverified_reply_errors(mains: dict[str, dict], failed_roots: dict[str, str],
                            sources: dict[str, set[str]]) -> tuple[list[dict], int]:
    """Keep precise failed tweet IDs internally, print concise Chinese by root."""
    errors: list[dict] = []
    blocked_count = 0
    for root_id, reason in sorted(failed_roots.items(), key=lambda pair: int(pair[0])):
        affected = sorted(
            ((tid, tweet) for tid, tweet in mains.items()
             if valid_id(obj(tweet.get('legacy')).get('conversation_id_str')) == root_id
             and valid_id(obj(tweet.get('legacy')).get('in_reply_to_status_id_str'))),
            key=lambda pair: int(pair[0]),
        )
        if not affected:
            errors.append({'kind': 'root_missing', 'root': root_id,
                           'reason': reason, 'tweet': '', 'sources': []})
            continue
        for tid, tweet in affected:
            errors.append({'kind': 'root_missing', 'root': root_id, 'reason': reason,
                           'tweet': tid, 'sources': sorted(sources.get(tid, []))})
            blocked_count += 1
    return errors, blocked_count


def verify_local_ancestor(core, json_dir: Path, existing: dict[str, Path],
                          missing_id: str) -> tuple[bool, bool]:
    """Check a missing Response ancestor against a REAL WTD primary JSON.

    `existing` indexes recognized on-disk tweets, but a filename can exist
    without having been indexed; inspect exact-ID snapshot filenames as well.
    Do not confuse a stale manifest entry or an unrelated nested tweet with
    a valid independent local snapshot. No file is written or altered.

    Returns (valid_local_post, matching_filename_exists).
    """
    paths: list[Path] = []
    indexed_path = existing.get(missing_id)
    if indexed_path is not None:
        paths.append(indexed_path)
    paths.extend(sorted(json_dir.glob(f'*_{missing_id}.json')))
    seen: set[Path] = set()
    found_file = False
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            continue
        found_file = True
        try:
            payload = core.load_snapshot_json(path)
            if not isinstance(payload, dict):
                continue
            # Core's finder may fall back to a different tweet in malformed
            # input. Explicitly require the primary to have the wanted ID.
            url = core.reconstruct_original_url('i', missing_id)
            node = core.find_primary_tweet_node(payload, url)
            if not isinstance(node, dict):
                continue
            meta = core._extract_metadata_from_node(node)
            if valid_id(meta.get('tweet_id')) == missing_id:
                return True, True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False, found_file


def missing_ancestor_warning(tid: str, tweet: dict, missing_ancestor_id: str,
                             sources: dict[str, set[str]], local_invalid: bool = False) -> dict:
    """Retain affected reply and source for grouped diagnostic output."""
    return {'kind': 'ancestor_missing', 'tweet': tid,
            'missing': missing_ancestor_id, 'local_invalid': local_invalid,
            'sources': sorted(sources.get(tid, []))}


def _short_source_labels(paths: set[str] | list[str]) -> list[str]:
    """Show only filenames; disambiguate equal basenames from different folders."""
    paths = sorted({str(p) for p in paths if p})
    by_name: dict[str, list[Path]] = defaultdict(list)
    for value in paths:
        p = Path(value)
        # Windows paths don't have native separators on Linux during tests.
        name = value.replace('\\', '/').rsplit('/', 1)[-1]
        by_name[name.casefold()].append(p)
    labels = []
    for name, items in sorted(by_name.items()):
        if len(items) == 1:
            labels.append(str(items[0]).replace('\\', '/').rsplit('/', 1)[-1])
        else:
            # Very rare duplicate names: show the final containing folder too.
            for p in items:
                value = str(p).replace('\\', '/')
                parts = value.split('/')
                labels.append('/'.join(parts[-2:]) if len(parts) >= 2 else value)
    return labels


def _root_error_in_chinese(reason: str) -> str:
    if reason.startswith('no local WTD root snapshot'):
        return '本地没有可验证的根推文'
    if 'differs from target' in reason:
        return '本地根推文作者与目标账号不符'
    if 'authors disagree' in reason:
        return '根推文的作者信息互相矛盾'
    if reason.startswith('local root not usable'):
        return '本地根推文数据无法通过校验'
    return '根推文验证失败'


def show_diagnostics(info: list, warnings: list, errors: list,
                     verified_roots: int = 0, normal_context: int = 0,
                     local_ancestor_count: int = 0,
                     local_ancestor_replies: int = 0) -> None:
    """Short console diagnostics grouped by root/ancestor and source filename.

    Ordinary local-root confirmations are one count, not a warning per reply.
    The source files themselves are never changed.
    """
    normal_parts = []
    if verified_roots or normal_context:
        normal_parts.append(f'已验证 {verified_roots} 条本地根推文，'
                            f'{normal_context} 条回复关联成功')
    if local_ancestor_count:
        normal_parts.append(f'另找到 {local_ancestor_count} 条本地上级推文，'
                            f'{local_ancestor_replies} 条回复关联成功')
    if normal_parts:
        print('正常：' + '；'.join(normal_parts) + '。')
    # Keep genuinely useful informational messages; no repeated success spam.
    if info:
        print(f'提示：{len(info)} 条其他说明（不影响归档）。')

    missing_groups: dict[str, dict] = {}
    other_warnings = []
    for item in warnings:
        if isinstance(item, dict) and item.get('kind') == 'ancestor_missing':
            g = missing_groups.setdefault(item['missing'], {'tweets': set(), 'sources': set(), 'local_invalid': False})
            g['tweets'].add(item['tweet'])
            g['sources'].update(item.get('sources', []))
            g['local_invalid'] = g['local_invalid'] or bool(item.get('local_invalid'))
        else:
            other_warnings.append(item)
    if missing_groups or other_warnings:
        affected = sum(len(g['tweets']) for g in missing_groups.values()) + len(other_warnings)
        print(f'\n警告：{affected} 条提示（不妨碍其余推文导入）')
        for missing, group in sorted(missing_groups.items(), key=lambda kv: int(kv[0])):
            status = ('本地有同 ID 的 JSON，但内容无法验证' if group['local_invalid']
                      else '当前 Response 和本地归档均未找到上级正文')
            print(f'  上级推文 {missing}：{status}；影响 {len(group["tweets"])} 条回复，ID 已保留')
            for filename in _short_source_labels(group['sources']):
                print(f'    - {filename}')
        for item in other_warnings:
            if isinstance(item, dict):
                print(f'  {item.get("reason", "需检查的输入文件")}:')
                for filename in _short_source_labels(item.get('sources', [])):
                    print(f'    - {filename}')
            else:
                print(f'  {item}')

    failed_roots: dict[str, dict] = {}
    other_errors = []
    for item in errors:
        if isinstance(item, dict) and item.get('kind') == 'root_missing':
            g = failed_roots.setdefault(item['root'],
                    {'tweets': set(), 'sources': set(), 'reason': item.get('reason', '')})
            if item.get('tweet'):
                g['tweets'].add(item['tweet'])
            g['sources'].update(item.get('sources', []))
        else:
            other_errors.append(item)
    if failed_roots or other_errors:
        affected = sum(len(g['tweets']) for g in failed_roots.values()) + len(other_errors)
        print(f'\n错误：{affected} 条推文/文件未正常导入')
        for root, group in sorted(failed_roots.items(), key=lambda kv: int(kv[0])):
            print(f'  根推文 {root}：{_root_error_in_chinese(group["reason"])}，'
                  f'{len(group["tweets"])} 条回复未导入')
            for filename in _short_source_labels(group['sources']):
                print(f'    - {filename}')
        for item in other_errors:
            if isinstance(item, dict):
                print(f'  {item.get("reason", "输入或转换失败")}:')
                for filename in _short_source_labels(item.get('sources', [])):
                    print(f'    - {filename}')
            else:
                print(f'  {item}')


def update_manifest(core, manifest: dict, asset_dir: Path, json_dir: Path, target: str, newly_added: list):
    """Preserve existing entries, add unlisted files, and repair *missing* pointers.

    Identity is the tweet ID, not JSON filename. A record listed in an old
    manifest but absent from disk can be re-imported without creating a second
    manifest entry for that same tweet.
    """
    saved = deepcopy(manifest)
    by_id: dict[str, int] = {}
    registered_names = set()
    for index, item in enumerate(saved['snapshots']):
        if not isinstance(item, dict):
            continue
        filename = string(item.get('json_filename'))
        if filename:
            registered_names.add(filename)
        url_match = re.search(r'/status/(\d+)', string(item.get('original_url')))
        parsed = core.parse_snapshot_filename(filename)
        tid = (url_match.group(1) if url_match else '') or (parsed[1] if parsed else '')
        if tid:
            by_id.setdefault(tid, index)

    # Enumerate actual valid snapshot files, not just files added this run.
    # This brings previously-unlisted local JSON into WTD's manifest as well.
    for path in sorted(json_dir.glob('*.json')):
        parsed = core.parse_snapshot_filename(path.name)
        if not parsed:
            continue
        ts, tid = parsed
        payload = core.load_snapshot_json(path)
        if not isinstance(payload, dict):
            continue
        meta = core.extract_tweet_metadata(payload, core.reconstruct_original_url('i', tid))
        if string(meta.get('tweet_id')) != tid:
            continue
        author = ''
        data = obj(payload.get('data'))
        aid = string(data.get('author_id'))
        for profile in sequence(obj(payload.get('includes')).get('users')):
            if string(obj(profile).get('id')) == aid:
                author = string(obj(profile).get('username'))
                break
        origin = core.reconstruct_original_url(author or 'i', tid)
        if tid in by_id:
            entry = saved['snapshots'][by_id[tid]]
            listed = string(entry.get('json_filename'))
            # Repair only a missing source file; never replace an existing
            # manifest pointer that may be an intentional historical version.
            if not listed or not (json_dir / listed).is_file():
                entry.update({'timestamp': ts, 'original_url': origin,
                              'json_filename': path.name})
                registered_names.add(path.name)
            continue
        if path.name in registered_names:
            continue
        saved['snapshots'].append({'timestamp': ts, 'original_url': origin,
                                   'json_filename': path.name})
        by_id[tid] = len(saved['snapshots']) - 1
        registered_names.add(path.name)

    # Update the manifest atomically. Do not create .bak backup files.
    # A no-op import does not rewrite an already-current manifest.
    if saved == manifest:
        return len(saved['snapshots'])
    manifest_path = core.snapshot_manifest_path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=asset_dir, encoding='utf-8',
                                         prefix='.browser-manifest-', suffix='.tmp',
                                         delete=False) as fh:
            tmp = Path(fh.name)
            json.dump(saved, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, manifest_path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return len(saved['snapshots'])


def main(argv=None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    # `scan USER response.txt` is a readable command; `--scan` is an alias.
    # Neither requires an --apply flag: the normal/default action is import.
    scan_command = bool(raw_args and raw_args[0] == 'scan')
    if scan_command:
        raw_args.pop(0)
    parser = argparse.ArgumentParser(
        description='Import captured X GraphQL Responses into complete WTD archives '
                    '(JSON, media, HTML, sorted views and index). Default: import.')
    parser.add_argument('twitter_username', help='Target account archived by WTD')
    parser.add_argument('responses', nargs='+', metavar='RESPONSE', help='Response JSON/TXT file(s), or folders')
    parser.add_argument('--scan', action='store_true', help='Read-only preview; write/download/render nothing (alias: scan USER RESPONSE)')
    parser.add_argument('--json-only', action='store_true', help='Only import JSON; intentionally skip all media and HTML steps')
    parser.add_argument('--repair-existing-context', action='store_true',
                        help='Additionally repair missing parent context in Response-matched browser-imported JSON')
    parser.add_argument('--no-auto-repair-existing', action='store_true',
                        help='Disable automatic local-only repair of existing descendant replies after importing new parents')
    parser.add_argument('--workers', type=int, default=8, metavar='N', help='WTD media/render workers (default: 8)')
    parser.add_argument('--exclude-ids', nargs='+', action='append', metavar='TWEET_ID',
                        help='Exclude listed tweet IDs from NEW independent snapshots; '
                             'keep them available as reply/quote context. '
                             'Use after RESPONSE paths; can be repeated or comma-separated.')
    args = parser.parse_args(raw_args)
    args.scan = args.scan or scan_command
    if not 1 <= args.workers <= 64:
        parser.error('--workers must be between 1 and 64')
    target = args.twitter_username.strip().lstrip('@')
    if not USER_RE.fullmatch(target):
        parser.error('Invalid X username')
    excluded_ids: set[str] = set()
    for group in args.exclude_ids or []:
        for item in group:
            for token in item.split(','):
                tid = token.strip()
                if not ID_RE.fullmatch(tid):
                    parser.error(f'Invalid --exclude-ids tweet ID: {tid!r}')
                excluded_ids.add(tid)
    try:
        files = collect_response_files(args.responses)
        mains, quoted, sources, warnings, errors, stats = read_responses(files)
        info: list[str] = []
        # Import core only at runtime, from the same folder as this helper.
        try:
            import download_archive as core
        except ImportError as exc:
            raise ImportErrorSafe('Put this script next to the patched download_archive.py; import failed: ' + str(exc)) from exc
        output_path, asset_dir, media_dir, json_dir, asset_name = core.build_output_paths(target)
        manifest, existing, occupied = collect_existing(core, asset_dir, json_dir, target)
        all_posts = {**quoted, **mains}
        context_resolver = LocalContextResolver(core, json_dir, existing)
        existing_context_repairs: list[tuple[str, Path, dict, dict]] = []
        repaired_new_context = 0
        unresolved_context = 0
        target_posts = {tid: tweet for tid, tweet in mains.items()
                        if user_name(tweet).casefold() == target.casefold()}
        verified_local_roots: dict[str, str] = {}
        failed_local_roots: dict[str, str] = {}
        blocked_replies = 0
        if target_posts:
            target_ids = {user_id(tweet) for tweet in target_posts.values()}
            if len(target_ids) != 1:
                raise ImportErrorSafe(
                    'The target username maps to multiple author IDs in the inputs: '
                    + ', '.join(sorted(target_ids)) + '; refusing import.')
            confirmed_author_id = next(iter(target_ids))
            print(f'已验证目标账号：@{target}')
        else:
            # Deleted/unavailable roots do not appear in TweetDetail responses.
            # Verify an on-disk root instead of accepting mere @mentions.
            verified_local_roots, failed_local_roots = verify_local_roots(
                core, mains, existing, target)
            failed_errors, blocked_replies = unverified_reply_errors(
                mains, failed_local_roots, sources)
            errors.extend(failed_errors)
            if not verified_local_roots:
                show_diagnostics(info, warnings, errors)
                raise ImportErrorSafe(
                    f'响应中没有 @{target} 本人推文，也没有可验证的本地根推文；'
                    f'{blocked_replies} 条回复无法导入。')
            confirmed_author_id = next(iter(verified_local_roots.values()))
            # One summary later; do not print every verified root ID here.
        selected, roles, scope_stats = choose_conversation_posts(
            mains, target, confirmed_author_id, verified_local_roots)
        stats.update(scope_stats)
        stats['unverified_roots'] = len(failed_local_roots)
        stats['unverified_replies'] = blocked_replies
        # Exclusion is a per-primary-snapshot filter, never a mutation of the
        # source response or its relationship/context objects.  Selection is
        # done FIRST so excluded parents still anchor their descendants.
        actual_exclusions = excluded_ids.intersection(selected)
        unknown_exclusions = excluded_ids - set(mains)
        out_of_scope_exclusions = excluded_ids.intersection(mains.keys()) - set(selected)
        for tid in sorted(unknown_exclusions, key=int):
            warnings.append({'kind': 'excluded_unknown', 'reason': '排除 ID ' + tid + ' 不在本次响应中', 'sources': []})
        for tid in sorted(out_of_scope_exclusions, key=int):
            info.append(f"{tid}: excluded ID already outside this archive's conversation scope")
        candidates = []
        local_ancestor_cache: dict[str, tuple[bool, bool]] = {}
        local_ancestor_ids: set[str] = set()
        for tid, tweet in sorted(selected.items(), key=lambda item: int(item[0])):
            # Descendants retain their own authors and IDs. A tweet with the
            # target screen_name but a conflicting account ID is never allowed.
            if (user_name(tweet).casefold() == target.casefold()
                    and user_id(tweet) != confirmed_author_id):
                stats['author_id_mismatch'] += 1
                errors.append({'kind': 'invalid', 'sources': sorted(sources.get(tid, [])), 'reason': '推文 ' + tid + ' 的作者 ID 与目标账号不一致，未导入'})
                continue
            if verified_local_roots:
                legacy = obj(tweet.get('legacy'))
                parent_id = valid_id(legacy.get('in_reply_to_status_id_str'))
                stated_parent_author = valid_id(legacy.get('in_reply_to_user_id_str'))
                if (parent_id in verified_local_roots and stated_parent_author
                        and stated_parent_author != verified_local_roots[parent_id]):
                    stats['invalid_reply_links'] += 1
                    errors.append({'kind': 'invalid', 'sources': sorted(sources.get(tid, [])), 'reason': '推文 ' + tid + ' 的上级作者 ID 矛盾，未导入'})
                    continue
            relation_issue = reply_integrity_issue(tweet, all_posts)
            if relation_issue:
                stats['invalid_reply_links'] += 1
                errors.append({'kind': 'invalid', 'sources': sorted(sources.get(tid, [])), 'reason': '推文 ' + tid + ' 的回复关系有冲突，未导入'})
                continue
            parents_captured, missing_parent = available_reply_ancestors(tweet, all_posts)
            if missing_parent:
                if missing_parent in verified_local_roots:
                    stats['local_root_context'] += 1
                    # Expected: the existing root is verified and remains unchanged.
                    # Count once, never flood the console with per-reply info.
                else:
                    # The Response did not include this intermediate ancestor.
                    # Before warning, check real local WTD JSON: only roots
                    # were checked here in older versions, creating false alarms.
                    if missing_parent not in local_ancestor_cache:
                        local_ancestor_cache[missing_parent] = verify_local_ancestor(
                            core, json_dir, existing, missing_parent)
                    is_local, filename_exists = local_ancestor_cache[missing_parent]
                    if is_local:
                        local_ancestor_ids.add(missing_parent)
                        stats['local_ancestor_replies'] += 1
                    else:
                        stats['missing_ancestor_context'] += 1
                        warnings.append(missing_ancestor_warning(
                            tid, tweet, missing_parent, sources,
                            local_invalid=filename_exists))
            if 'retweeted_status_result' in tweet or 'retweeted_status' in obj(tweet.get('legacy')):
                stats['reposts_ignored'] += 1
                continue
            # An excluded post can still be embedded by build_payload() as an
            # ancestor or quoted post in a different primary snapshot.  Never
            # remove it from all_posts or prune its downstream descendants.
            if tid in excluded_ids:
                stats['excluded_independent'] += 1
                if tid in occupied:
                    stats['excluded_already_existing'] += 1
                continue
            if tid in occupied:
                stats['existing'] += 1
                if args.repair_existing_context:
                    path = existing.get(tid)
                    if path and path.is_file():
                        original = core.load_snapshot_json(path)
                        if isinstance(original, dict) and (obj(original.get('_pale_fire')).get('source')
                                                           == 'browser_graphql_response'):
                            # Avoid altering archived Wayback/X API records or other users' data.
                            main = obj(original.get('data'))
                            if (valid_id(main.get('id')) == tid
                                    and valid_id(main.get('author_id')) == user_id(tweet)):
                                enriched = deepcopy(original)
                                changes, unresolved = enrich_reply_context(
                                    enriched, all_posts, context_resolver, core)
                                if changes:
                                    existing_context_repairs.append((tid, path, original, enriched))
                                if unresolved:
                                    unresolved_context += 1
                                    warnings.append(missing_ancestor_warning(
                                        tid, tweet, unresolved, sources))
                continue
            try:
                snapshot = build_payload(tweet, all_posts, core, archive_user=target)
                changes, unresolved = enrich_reply_context(
                    snapshot, all_posts, context_resolver, core)
                if changes:
                    repaired_new_context += 1
                if unresolved:
                    unresolved_context += 1
                    if not any(isinstance(w, dict) and w.get('kind') == 'ancestor_missing'
                               and w.get('tweet') == tid and w.get('missing') == unresolved
                               for w in warnings):
                        warnings.append(missing_ancestor_warning(
                            tid, tweet, unresolved, sources))
                # WTD has its own timestamp and filename construction.
                timestamp = core.parse_x_api_created_at_timestamp(snapshot['data']['created_at'])
                original_url = core.reconstruct_original_url(user_name(tweet), tid)
                dest = core.build_json_output_path(json_dir, timestamp, original_url, {'tweet_id': tid})
                if dest.exists():
                    stats['occupied_paths'] += 1
                    continue
                # Validate through WTD's own metadata/content extraction before writing.
                meta = core.extract_tweet_metadata(snapshot, original_url)
                text, media, _, _ = core.extract_tweet_content(
                    snapshot, original_url,
                    x_api_use_json_cache=True,
                    x_api_reply_chain_depth=0,
                    allow_reply_chain_media_lookup=False,
                    reply_chain_lookup_context=core.ReplyChainLookupContext(
                        use_local_json=False, use_wayback=False, allow_conversation_wayback=False))
                if string(meta.get('tweet_id')) != tid or (not text and not media):
                    raise ImportErrorSafe('WTD did not recognize the converted primary tweet')
                candidates.append((tid, snapshot, timestamp, original_url, dest, parents_captured, missing_parent, roles[tid]))
            except Exception as exc:
                errors.append({'kind': 'conversion', 'sources': sorted(sources.get(tid, [])), 'reason': '推文 ' + tid + ' 转换失败（' + str(exc) + '），未导入'})
                stats['conversion_errors'] += 1
        # Every discovered timeline tweet is counted exactly once: selected
        # for this archive, blocked by local-root verification, or unrelated.
        # In particular, a missing-root reply is NOT an "already existing" or
        # "duplicate" tweet and must not silently disappear from the summary.
        outside_ids = set(mains).difference(selected)
        failed_root_ids = {
            string(e.get('tweet')) for e in errors
            if isinstance(e, dict) and e.get('kind') == 'root_missing'
            and string(e.get('tweet')) in outside_ids
        }
        unrelated_ids = outside_ids.difference(failed_root_ids)
        print(f'\n账号：@{target}')
        print(f'响应文件：{stats["files_parsed"]}/{len(files)} 份有效；'
              f'发现 {len(mains)} 条不同的推文（按推文 ID 去重）')
        repeated = stats['timeline_items'] - len(sources)
        if repeated:
            print(f'响应内重复出现：{repeated} 次（已按推文 ID 合并，不计入上述发现数）')
        # State the actual reasons instead of opaque "in scope / out of scope"
        # categories. Verification failures include both unverified roots and
        # invalid author/reply relationships among otherwise selected tweets.
        verification_failed = (len(failed_root_ids)
                               + stats['author_id_mismatch']
                               + stats['invalid_reply_links'])
        print(f'准备新增：{len(candidates)} 条；本地已有：{stats["existing"]} 条；'
              f'主动排除：{stats["excluded_independent"]} 条；'
              f'校验失败：{verification_failed} 条；无关对话：{len(unrelated_ids)} 条')
        # Only unusual cases get an extra line. Keep every discovered tweet
        # accounted for; do not disguise conversion errors as verification errors.
        other_skipped = (len(mains) - len(candidates) - stats['existing']
                         - stats['excluded_independent'] - verification_failed
                         - len(unrelated_ids))
        if other_skipped:
            print(f'其他未新增：{other_skipped} 条（如转换失败或文件名冲突，详见错误提示）。')
        if excluded_ids:
            print(f'筛选：指定排除 {len(excluded_ids)} 个 ID，'
                  f'本次实际排除 {stats["excluded_independent"]} 条')
            if stats['excluded_already_existing']:
                print(f'  其中 {stats["excluded_already_existing"]} 条已有本地文件，未删除。')
        show_diagnostics(info, warnings, errors,
                         verified_roots=len(verified_local_roots),
                         normal_context=stats['local_root_context'],
                         local_ancestor_count=len(local_ancestor_ids),
                         local_ancestor_replies=stats['local_ancestor_replies'])
        if args.repair_existing_context:
            print(f'上级引用待修复：{len(existing_context_repairs)} 条已有 JSON（仅处理浏览器导入的文件）。')
        if args.scan:
            print('扫描完成：没有修改归档文件。')
            if blocked_replies:
                print(f'扫描结果：{blocked_replies} 条回复因根推文无法验证而未纳入。')
            return 3 if errors else 0

        # When all eligible primary posts are excluded and no WTD snapshots
        # exist yet, there is nothing to import or render. Do not initialize
        # an empty archive that would subsequently fail HTML rendering.
        if not candidates and not any(json_dir.glob('*.json')):
            print('没有可新增的推文，本地也没有归档文件。')
            return 0

        # Stage 1: save *only* new JSON files. Existing valid JSON is never
        # replaced; the existing manifest is merged atomically without .bak files.
        json_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for tid, snapshot, ts, original_url, dest, _, _, _ in candidates:
            try:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                errors.append({'kind': 'write', 'sources': sorted(sources.get(tid, [])), 'reason': '推文 ' + tid + ' 的目标文件已存在，未覆盖'})
                print(f'错误：推文 {tid} 的目标文件已存在，已跳过（没有覆盖）。')
                continue
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
                    json.dump(snapshot, fh, ensure_ascii=False, indent=2)
                    fh.write('\n')
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            written.append(core.SnapshotRecord(timestamp=ts, original_url=original_url, json_path=dest))

        repaired_count = 0
        for tid, path, original, enriched in existing_context_repairs:
            # Strict no-overwrite safety if another program has changed the file
            # since this process inspected it.
            current = core.load_snapshot_json(path)
            if current != original:
                errors.append({'kind': 'write', 'sources': sorted(sources.get(tid, [])),
                               'reason': '推文 ' + tid + ' 在修复期间发生变化，已跳过'})
                continue
            try:
                _write_enriched_existing_json(core, path, original, enriched, tid)
                repaired_count += 1
            except (OSError, ImportErrorSafe) as exc:
                errors.append({'kind': 'write', 'sources': sorted(sources.get(tid, [])),
                               'reason': '推文 ' + tid + ' 上级引用修复失败：' + str(exc)})
        if repaired_new_context or repaired_count or unresolved_context:
            print(f'上级引用：新导入补全 {repaired_new_context} 条；'
                  f'旧 JSON 修复 {repaired_count} 条；'
                  f'仍缺上级 {unresolved_context} 条（保留原引用 ID）。')
        total = update_manifest(core, manifest, asset_dir, json_dir, target, written)
        if not args.no_auto_repair_existing and written:
            print('自动检查本次新增推文所影响的已有下级回复（仅本地 JSON）……', flush=True)
            affected_count, auto_repaired, auto_failed = auto_repair_existing_descendants(
                core, json_dir, existing, written, all_posts)
            print(f'下级回复自动补链：关联 {affected_count} 条；'
                  f'实际修复 {auto_repaired} 条；失败 {auto_failed} 条。')
            if auto_failed:
                errors.append({'kind': 'auto_repair', 'sources': [],
                               'reason': f'{auto_failed} 条已有下级回复自动补链失败；原文件已保护，请检查警告'})
        print(f'JSON 导入：新增 {len(written)} 条，原有 {stats["existing"]} 条；'
              f'归档清单共 {total} 条。')
        if args.json_only:
            print('仅 JSON 模式已完成；未下载媒体、未生成 HTML。')
            if errors:
                print(f'部分成功：新增 {len(written)} 条；待检查问题 {len(errors)} 项；'
                      f'根推文验证失败跳过 {blocked_replies} 条。')
            return 3 if errors else 0

        # Stage 2: WTD's native HTML pipeline consumes ALL existing + newly
        # imported records, not merely written records. This updates old roots'
        # HTML to include new downstream replies, and regenerates all WTD views.
        # The media policy permits ONLY direct media URLs already in saved JSON,
        # never paid X API, Wayback CDX lookups or Wayback media fallbacks.
        print('正在运行 WTD：处理媒体、重建 HTML 和索引……', flush=True)
        try:
            records = core.load_snapshot_records(asset_dir, json_dir, target)
            available = [record for record in records if record.json_path.is_file()]
            if not available:
                raise ImportErrorSafe('No valid local JSON snapshots are available for HTML generation')
            policy = replace(core.MEDIA_FETCH_FULL,
                             allow_wayback_fallback=False,
                             allow_closest_cdx_lookup=False)
            written_paths = core.build_archives_from_snapshot_records(
                available, target, output_path, asset_dir, args.workers,
                resume=True,
                download_missing_media=True,
                x_bearer_token='',
                x_api_reply_chain_depth=0,
                x_api_reply_chain_use_wayback=False,
                media_fetch_policy=policy,
            )
            expected = core.get_archive_output_jobs(output_path)
            expected_html = [job[2] for job in expected] + [output_path.with_name('index.html')]
            missing_html = [str(path) for path in expected_html if not path.is_file()]
            if missing_html:
                raise ImportErrorSafe('WTD reported HTML generation, but outputs are absent: '
                                      + ', '.join(missing_html))
            print(f'WTD 处理完成：{len(available)} 条本地推文，'
                  f'{len(written_paths)} 个 HTML/索引输出。')
        except Exception as exc:
            print(f'错误：WTD 媒体或网页处理未完成：{type(exc).__name__}: {exc}', file=sys.stderr)
            print('已写入的 JSON 和清单仍保留；可以重运行相同命令补做。', file=sys.stderr)
            return 2
        print('归档流程完成：JSON、媒体、HTML 和索引已处理；已有 JSON 仅进行验证后的上级引用补全。')
        if errors:
            print(f'部分成功：新增 {len(written)} 条；待检查问题 {len(errors)} 项；'
                  f'根推文验证失败跳过 {blocked_replies} 条。')
        return 3 if errors else 0
    except (OSError, ImportErrorSafe) as exc:
        print('错误：', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
