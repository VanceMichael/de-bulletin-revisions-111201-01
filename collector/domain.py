from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


class Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.body = []
        self.canonical = None
        self.in_title = False
        self.in_article = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'h1':
            self.in_title = True
        if tag == 'article':
            self.in_article = True
        if tag == 'link' and values.get('rel') == 'canonical':
            self.canonical = values.get('href')

    def handle_endtag(self, tag):
        if tag == 'h1':
            self.in_title = False
        if tag == 'article':
            self.in_article = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.in_article:
            self.body.append(data)


def parse_page(url, body):
    parser = Article()
    parser.feed(body)
    title = ''.join(parser.title).strip()
    content = ' '.join(part.strip() for part in parser.body if part.strip())
    if not title or not content:
        raise ValueError('正文或标题缺失')
    return dict(key=url, title=title, content=content,
                declared_canonical=urljoin(url, parser.canonical) if parser.canonical else None)


def collect(payload):
    return [parse_page(page['url'], page['html']) for page in payload['pages']]


def content_digest(title, content):
    """版本身份按解析后的正文内容确定；不同页面的相同正文视为同一版本。"""
    return sha256(f'{title}\n{content}'.encode('utf-8')).hexdigest()


# 常见跟踪参数：采集身份时去掉；版本号等业务参数一律保留。
TRACKING_PARAMETERS = {
    'fbclid', 'gclid', 'dclid', 'yclid', 'spm', 'igshid',
    'mc_cid', 'mc_eid', '_ga', '_gl', 'oly_anon_id', 'oly_enc_id',
    'vero_id', 'wickedid', 'ttclid',
}


def is_tracking_parameter(name):
    lowered = name.lower()
    return lowered.startswith('utm_') or lowered in TRACKING_PARAMETERS


def normalize_url(url):
    """归一化链接身份：去跟踪参数与碎片，保留版本号等查询参数。"""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or '').lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == 'http' and port == 80) or (scheme == 'https' and port == 443)):
        netloc = f'{host}:{port}'
    else:
        netloc = host
    kept = sorted(
        (name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_parameter(name))
    return urlunsplit((scheme, netloc, parts.path, urlencode(kept), ''))


def origin_of(url):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == 'https' else 80 if scheme == 'http' else None
    return (scheme, (parts.hostname or '').lower(), port)


def is_allowed_origin(url, allow_origins):
    allowed = {origin_of(origin) for origin in allow_origins}
    return origin_of(url) in allowed


def resolve_identity(link_url, declared_canonical, allow_origins):
    """以发布单位原始条目为主线确定条目身份。

    返回 (main_url, relation, note)。站点声明的 canonical 仅在其目标
    属于配置允许的来源时采信；否则按本链接自身建立条目。标题不参与合并。
    """
    if declared_canonical:
        candidate = normalize_url(declared_canonical)
        if is_allowed_origin(candidate, allow_origins):
            if candidate == link_url:
                return link_url, 'original', None
            return candidate, 'reprint', None
        return link_url, 'original', '未采信来源声明的 canonical（目标不在允许来源内）'
    return link_url, 'original', None
