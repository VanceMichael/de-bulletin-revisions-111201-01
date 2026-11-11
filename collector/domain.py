from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit


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
