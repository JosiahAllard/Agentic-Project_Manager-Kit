"""kitlib.hubsite.pagecheck: what a published page's HTML may hold (docs/action-hub.md
"pages/<id> and page_html/<id>").

  MAX_BYTES     the largest body, in bytes of UTF-8
  MERMAID_SAYS  the words every refusal of unrendered Mermaid carries
  check(html)   why html cannot be published, as one sentence, or None when it can

A page is static HTML the Docs tab shows in an iframe with sandbox="" and a Content-Security-
Policy that loads nothing from outside the page. That iframe is the guarantee; this check is the
second line, so a page that would break out of it, or would lean on what it blocks, is refused
whole. Nothing is stripped or rewritten: re-writing HTML risks this parser and the browser reading
the markup differently, and a refusal adds no markup.

Refused:
  - a body that is not a string, holds a lone surrogate, or is over MAX_BYTES as UTF-8;
  - the elements of TAGS (script, iframe, frame, frameset, object, embed, applet, portal, link,
    meta, base, form);
  - an on* event attribute, and ping or formaction;
  - href or xlink:href that is not a #fragment, except a data: one on svg image or feImage;
  - src, poster, background or action that is not data:, and srcset at all (src with a data:
    image is the way);
  - an svg animate or set whose attributeName is href (it would make a link the check never saw);
  - CSS url() that is not data: or a #fragment, @import, any backslash (a CSS escape can spell
    url or @import) and the image functions of CSS_FUNCTIONS, in a style element or in any
    attribute but the plain-text ones of TEXT_ATTRS (which keep the url() and @import rules);
  - in a style element's text, checked whole at its end: any `<` (inside svg or math a browser
    reads tags there, which this parser never sees) and any `&` (there a browser decodes a
    character reference, so `&#92;` would be a backslash this parser never sees);
  - a self-closing `<style/>`: HTML ignores the slash, so what follows would be CSS that this
    parser reads as text;
  - unrendered Mermaid: class mermaid or language-mermaid on any element, or a ```mermaid fence.
    The kit renders nothing: the publisher draws the diagram as inline svg (MERMAID_SAYS);
  - anywhere in the text, whatever the parser made of it, each of RAW: `<script`, `javascript:`,
    `http-equiv`, the raw-text and escapable elements this parser and a browser may end in
    different places (`<title`, `<textarea`, `<xmp`, `<noembed`, `<noframes`, `<plaintext`), and
    the markup whose end the two may find in different places (`<![cdata[`, `<!-->`, `<!--->`,
    `--!>`).
So the parser sees every tag and attribute a browser would build from the body. Kept: inline svg
(not its title element) with its style and foreignObject, style elements and attributes, data:
images. Nothing here prints, parses arguments or exits.
"""
import html.parser
import re

MAX_BYTES = 512 * 1024
MERMAID_SAYS = 'render it to svg first'
TAGS = ('script', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'portal', 'link',
        'meta', 'base', 'form')
DATA_ONLY = ('src', 'poster', 'background', 'action')
NEVER = ('srcset', 'ping', 'formaction')
LINKS = ('href', 'xlink:href')
LINK_DATA_TAGS = ('image', 'feimage')       # svg elements whose href may be a data: image
ANIMATES = ('animate', 'set')
RAW = ('<script', 'javascript:', 'http-equiv', '<title', '<textarea', '<xmp', '<noembed',
       '<noframes', '<plaintext', '<![cdata[', '<!-->', '<!--->', '--!>')
CSS_FUNCTIONS = ('image-set(', '-webkit-image-set(', 'image(')  # each can load an image by url
TEXT_ATTRS = ('title', 'alt')               # read only as text; so are aria-* (_text_attr)
STYLE_NEVER = ('<', '&')                    # in a style element's text (module docstring)

_SURROGATE = re.compile('[\ud800-\udfff]')
_URL = re.compile(r'url\(\s*([\'"]?)(.*?)\1\s*\)', re.I | re.S)
_SPACE = re.compile(r'[\x00-\x20]+')        # what a browser drops from a URL before it reads it
_MERMAID_CLASS = ('mermaid', 'language-mermaid')


class Refused(Exception):
    pass


def _url(value):
    return _SPACE.sub('', value or '').lower()


def _text_attr(name):
    return name in TEXT_ATTRS or name.startswith('aria-')


def _css(text, where, plain=False):
    """Refused when `text` is CSS that could load from outside the page. `plain`: an attribute
    read only as text, which is held to the url() and @import rules alone."""
    low = (text or '').lower()
    if '@import' in low:
        raise Refused('%s has @import: a page loads no other stylesheet' % where)
    for m in _URL.finditer(text or ''):
        u = _url(m.group(2))
        if not (u.startswith('data:') or u.startswith('#')):
            raise Refused('%s has url(%s): a page loads nothing from outside it, only data: '
                          'and #fragment urls' % (where, m.group(2).strip()[:80]))
    if plain:
        return
    if '\\' in low:
        raise Refused('%s has a backslash: a CSS escape could spell a url or @import the check '
                      'never saw, so a page has none' % where)
    squeezed = _SPACE.sub('', low)
    for fn in CSS_FUNCTIONS:
        if fn in squeezed:
            raise Refused('%s has %s): it loads an image, and a page loads nothing from '
                          'outside it; use url() with a data: image' % (where, fn))


class _Parser(html.parser.HTMLParser):
    def __init__(self):
        html.parser.HTMLParser.__init__(self, convert_charrefs=True)
        self._style = None                      # a style element's text so far, or None

    def handle_starttag(self, tag, attrs):
        self._tag(tag, attrs)
        if tag == 'style':
            self._style = []

    def handle_startendtag(self, tag, attrs):
        self._tag(tag, attrs)
        if tag == 'style':
            raise Refused('the page has a self-closing <style/>: HTML ignores the slash, so what '
                          'follows it would be CSS; write <style></style>')

    def handle_endtag(self, tag):
        if tag == 'style':
            self.end_style()

    def handle_data(self, data):
        if self._style is not None:
            self._style.append(data)

    def end_style(self):
        """Check the open style element's text whole (at its end tag, or at the body's end)."""
        if self._style is None:
            return
        text, self._style = ''.join(self._style), None
        for c in STYLE_NEVER:
            if c in text:
                raise Refused('a <style> element holds %r: inside svg or math a browser reads '
                              'it as markup this check never sees, so a style element has none'
                              % c)
        _css(text, 'a <style> element')

    def _tag(self, tag, attrs):
        if tag in TAGS:
            raise Refused('the page has a <%s> element: a page is static HTML that loads and '
                          'runs nothing' % tag)
        named = {}
        for name, value in attrs:
            value = value or ''
            named[name] = value
            where = 'the %s attribute of <%s>' % (name, tag)
            u = _url(value)
            if name.startswith('on'):
                raise Refused('%s is an event handler: a page runs no script' % where)
            if name in NEVER:
                raise Refused('%s is not allowed: a page loads nothing from outside it%s'
                              % (where, '; use src with a data: image' if name == 'srcset'
                                 else ''))
            if name in DATA_ONLY and not u.startswith('data:'):
                raise Refused('%s must be a data: url: a page loads nothing from outside it'
                              % where)
            if name in LINKS and not u.startswith('#') and not (
                    tag in LINK_DATA_TAGS and u.startswith('data:')):
                raise Refused('%s must be a #fragment: a page links nowhere outside it' % where)
            if name == 'class':
                classes = value.lower().split()
                if any(c in _MERMAID_CLASS for c in classes):
                    raise Refused('<%s class="%s"> is unrendered Mermaid: %s (inline svg)'
                                  % (tag, value.strip()[:80], MERMAID_SAYS))
            _css(value, where, plain=_text_attr(name))
        if tag in ANIMATES and named.get('attributename', '').strip().lower() in LINKS:
            raise Refused('an svg <%s> that changes href is not allowed: a page links nowhere '
                          'outside it' % tag)


def check(html):
    """Why html cannot be published (module docstring), as one sentence, or None."""
    if not isinstance(html, str):
        return 'html must be a string, not %s' % type(html).__name__
    m = _SURROGATE.search(html)
    if m:
        return ('html holds a lone surrogate (U+%04X) at character %d, half of a character '
                'that cannot be written' % (ord(m.group()), m.start()))
    size = len(html.encode('utf-8'))
    if size > MAX_BYTES:
        return 'html is %d bytes of UTF-8, over the %d a page may be' % (size, MAX_BYTES)
    low = html.lower()
    if '```mermaid' in low:
        return 'the page has a ```mermaid fence, unrendered Mermaid: %s (inline svg)' % MERMAID_SAYS
    parser = _Parser()
    try:
        parser.feed(html)
        parser.close()
        parser.end_style()                      # a style element the body never closed
    except Refused as e:
        return str(e)
    for word in RAW:
        if word in low:
            return 'the page holds %r: a page is static HTML that runs nothing' % word
    return None
