from datetime import datetime, timezone

from .domain import content_digest, normalize_url, parse_page, resolve_identity
from .transport import Client


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Tracker:
    """持续追踪：条件请求、版本切换、撤稿与失败区分。

    链接凭据（ETag/Last-Modified/本链接正文）全部落在数据库，进程内不保存
    状态，工作进程可随时退出或重启。正文先持久化（第一阶段），版本与索引头
    在单个事务内生效（第二阶段）；两阶段之间崩溃不会留下对外可见的半成品。
    """

    def __init__(self, store, client=None, allow_origins=(), resolver=None,
                 clock=utc_now, after_raw_saved=None):
        self.store = store
        self.client = client or Client()
        self.allow_origins = list(allow_origins)
        self.resolver = resolver or (lambda url: url)
        self.clock = clock
        # 故障注入钩子：仅用于演示与测试“原文保存后、索引更新前”的崩溃窗口。
        self.after_raw_saved = after_raw_saved

    def poll(self, url):
        link_url = normalize_url(url)
        link = self.store.get_link(link_url)
        headers = {}
        if link is not None:
            if link['etag']:
                headers['If-None-Match'] = link['etag']
            if link['last_modified']:
                headers['If-Modified-Since'] = link['last_modified']
        fetched_at = self.clock()
        try:
            status, response_headers, body = self.client.request(
                self.resolver(link_url), headers=headers)
        except Exception as error:
            self.store.commit_failure(link_url=link_url, status=0,
                                      detail=f'请求异常: {error}',
                                      fetched_at=fetched_at, source_date=None)
            return dict(outcome='failed', url=link_url, status=0, detail=str(error))
        lowered = {name.lower(): value for name, value in response_headers.items()}
        source_date = lowered.get('date')
        etag = lowered.get('etag')
        last_modified = lowered.get('last-modified')
        if status == 200:
            return self._apply_body(link_url, body, etag, last_modified,
                                    fetched_at, source_date, 200)
        if status == 304:
            return self._apply_not_modified(link_url, etag, last_modified,
                                            fetched_at, source_date)
        if status == 410:
            reason = body.decode('utf-8', errors='replace').strip() or '发布方返回 410'
            main_url, relation = self.store.link_identity(link_url)
            changed = self.store.commit_retraction(
                link_url=link_url, main_url=main_url, relation=relation, reason=reason,
                fetched_at=fetched_at, source_date=source_date, http_status=status)
            return dict(outcome='retracted', url=link_url, main_url=main_url,
                        changed=changed, reason=reason)
        self.store.commit_failure(link_url=link_url, status=status,
                                  detail=f'HTTP {status} 暂时抓取失败',
                                  fetched_at=fetched_at, source_date=source_date)
        return dict(outcome='failed', url=link_url, status=status)

    def _apply_not_modified(self, link_url, etag, last_modified, fetched_at, source_date):
        link = self.store.get_link(link_url)
        raw = self.store.get_raw(link['body_digest']) if link is not None and link['body_digest'] else None
        if raw is None:
            # 304 必须沿用本链接自己的正文；本链接没有缓存时不能借用其他转载页或主线。
            self.store.commit_failure(link_url=link_url, status=304,
                                      detail='来源返回 304，但本链接没有可沿用的正文',
                                      fetched_at=fetched_at, source_date=source_date)
            return dict(outcome='failed', url=link_url, status=304,
                        detail='304 无本链接缓存')
        return self._apply_body(link_url, raw, etag or link['etag'],
                                last_modified or link['last_modified'],
                                fetched_at, source_date, 304)

    def _apply_body(self, link_url, raw, etag, last_modified, fetched_at, source_date, http_status):
        digest = self.store.save_raw(raw)  # 第一阶段：正文持久化（幂等）
        if self.after_raw_saved is not None:
            self.after_raw_saved()  # 崩溃窗口：正文已保存、索引未更新
        try:
            page = parse_page(link_url, raw.decode('utf-8', errors='replace'))
        except ValueError as error:
            self.store.commit_failure(link_url=link_url, status=http_status, detail=str(error),
                                      fetched_at=fetched_at, source_date=source_date)
            return dict(outcome='failed', url=link_url, status=http_status, detail=str(error))
        main_url, relation, note = resolve_identity(
            link_url, page['declared_canonical'], self.allow_origins)
        result = self.store.commit_observation(
            link_url=link_url, main_url=main_url, relation=relation, digest=digest,
            content_key=content_digest(page['title'], page['content']),
            title=page['title'], content=page['content'], etag=etag,
            last_modified=last_modified, fetched_at=fetched_at,
            source_date=source_date, http_status=http_status, note=note)
        return dict(outcome=result['outcome'], url=link_url, main_url=main_url,
                    relation=relation, seq=result['seq'], switched=result['switched'],
                    status=http_status)
