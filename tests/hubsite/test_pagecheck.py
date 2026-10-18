"""kitlib.hubsite.pagecheck: what a published page may hold. Each refused sample names the rule it
breaks in the refusal; each kept sample is what a real page uses."""
import unittest

from kitlib.hubsite import pagecheck

# (sample, a word the refusal carries)
REFUSED = (
    ('<script>alert(1)</script>', '<script>'),
    ('<SCRIPT src="x.js"></SCRIPT>', '<script>'),
    ('<svg><script>alert(1)</script></svg>', '<script>'),
    ('<iframe src="data:text/html,x"></iframe>', '<iframe>'),
    ('<frameset><frame src="x"></frameset>', '<frameset>'),
    ('<object data="x.swf"></object>', '<object>'),
    ('<embed src="data:x">', '<embed>'),
    ('<link rel="prefetch" href="https://example.com/x">', '<link>'),
    ('<meta http-equiv="refresh" content="0;url=https://example.com">', '<meta>'),
    ('<meta charset="utf-8">', '<meta>'),
    ('<base href="https://example.com/">', '<base>'),
    ('<form action="data:x"><input></form>', '<form>'),
    ('<img src="data:x" onerror="alert(1)">', 'event handler'),
    ('<svg onload="alert(1)"></svg>', 'event handler'),
    ('<a href="https://example.com">x</a>', '#fragment'),
    ('<A HREF=" HTTPS://example.com">x</A>', '#fragment'),
    ('<a href="data:text/html,x">x</a>', '#fragment'),
    ('<a href="&#106;avascript:alert(1)">x</a>', '#fragment'),
    ('<svg><a xlink:href="https://example.com"><text>x</text></a></svg>', '#fragment'),
    ('<svg><use href="https://example.com/s.svg#i"/></svg>', '#fragment'),
    ('<svg><image href="https://example.com/x.png"/></svg>', '#fragment'),
    ('<a href="#x" ping="https://example.com">x</a>', 'ping'),
    ('<button formaction="https://example.com">x</button>', 'formaction'),
    ('<img src="https://example.com/x.png">', 'data:'),
    ('<img src="/x.png">', 'data:'),
    ('<video poster="x.png"></video>', 'data:'),
    ('<table background="x.png"></table>', 'data:'),
    ('<img srcset="data:image/png;base64,AA 1x">', 'srcset'),
    ('<svg><set attributeName="href" to="#x"/></svg>', 'href'),
    ('<svg><animate attributeName="xlink:href" values="#a;#b"/></svg>', 'href'),
    ('<style>body{background:url(https://example.com/x.png)}</style>', 'url('),
    ('<style>@import "x.css";</style>', '@import'),
    ('<style>@IMPORT url(data:text/css,x);</style>', '@import'),
    ('<div style="background: URL( \'//example.com/x\' )">x</div>', 'url('),
    ('<svg><rect fill="url(https://example.com/g.svg#g)"/></svg>', 'url('),
    ('<div class="mermaid">graph TD; A-->B</div>', pagecheck.MERMAID_SAYS),
    ('<pre class="Mermaid">graph TD</pre>', pagecheck.MERMAID_SAYS),
    ('<pre><code class="language-mermaid">graph TD</code></pre>', pagecheck.MERMAID_SAYS),
    ('<pre>```mermaid\ngraph TD\n```</pre>', pagecheck.MERMAID_SAYS),
    ('<p>javascript: is a scheme</p>', 'javascript:'),
    ('<p>&lt;script&gt; is text, but <!-- <script --></p>', '<script'),
    ('<p http-equiv="x">x</p>', 'http-equiv'),
)

# Markup this parser and a browser could read differently, each refused whole: what a browser
# would build from it (a link, a stylesheet) is markup the check would otherwise never see.
DIFFERENTIAL = (
    ('<svg><style><p></p><a href="https://x">click</a><link rel=dns-prefetch '
     'href=//x.example></style></svg>', "'<'"),
    ('<math><style><p></p><a href="https://x">click</a><link rel=dns-prefetch '
     'href=//x.example></style></math>', "'<'"),
    ('<svg><style>a{background:&#117;rl(https://x/a.png)}</style></svg>', "'&'"),
    ('<style>a{background:\\75rl(https://x/a.png)}</style>', 'backslash'),
    ('<div style="background:\\75rl(https://x/a.png)">x</div>', 'backslash'),
    ('<style>@\\69mport "https://x/a.css";</style>', 'backslash'),
    ('<style>a{background:image-set("https://x")}</style>', 'image-set('),
    ('<div style="background-image: image-set( \'https://x\' 1x)">x</div>', 'image-set('),
    ('<style>a{background:-webkit-image-set("https://x" 1x)}</style>', 'image-set('),
    ('<svg><rect style="fill:image(\'https://x/a.png\')"/></svg>', 'image('),
    ('<style/>a{background:url(https://x/a.png)}</style>', 'self-closing'),
    ('<style/><p>x</p>', 'self-closing'),
    ('<style>a{background:url(https://x/a.png)}', 'url('),        # never closed: checked at the end
    # Where a browser ends these, Python 3.14's parser ends them too and refuses the link it
    # finds (an older parser may not); RAW_ONLY below shows RAW refuses each by itself.
    ('<title><b title="</title><a href=https://x>x</a>">', None),
    ('<textarea><b title="</textarea><a href=https://x>x</a>">', None),
    ('<xmp><b title="</xmp><a href=https://x>x</a>">', None),
    ('<noembed><b title="</noembed><a href=https://x>x</a>">', None),
    ('<noframes><b title="</noframes><a href=https://x>x</a>">', None),
    ('<p><!--><a href="https://x">x</a>--></p>', None),
    ('<p><!---><a href="https://x">x</a>--></p>', None),
    ('<p><!-- --!><a href="https://x">x</a>--></p>', None),
    ('<p><![CDATA[><a href="https://x">x</a>]]></p>', None),
)

# Each word of RAW in markup that holds nothing else the check refuses.
RAW_ONLY = (
    ('<svg><title>x</title></svg>', '<title'),
    ('<textarea>x</textarea>', '<textarea'),
    ('<xmp>x</xmp>', '<xmp'),
    ('<noembed>x</noembed>', '<noembed'),
    ('<noframes>x</noframes>', '<noframes'),
    ('<plaintext>x', '<plaintext'),
    ('<p><![CDATA[x]]></p>', '<![cdata['),
    ('<p><!-->x</p>', '<!-->'),
    ('<p><!--->x</p>', '<!--->'),
    ('<p><!-- x --!>y</p>', '--!>'),
)

MERMAID_CLI_SVG = (
    '<svg aria-roledescription="flowchart-v2" role="graphics-document document" '
    'viewBox="-8 -8 200 120" style="max-width: 200px;" xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" id="my-svg">'
    '<style>#my-svg{font-family:"trebuchet ms",verdana,arial,sans-serif;fill:#333;}'
    '#my-svg .node rect{fill:#ECECFF;stroke:#9370DB;}</style>'
    '<defs><marker id="my-svg_flowchart-v2-pointEnd" viewBox="0 0 10 10" refX="5" refY="5">'
    '<path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>'
    '<g class="root"><g class="edgePaths"><path d="M10,10L90,10" '
    'marker-end="url(#my-svg_flowchart-v2-pointEnd)"/></g>'
    '<g class="node default" id="flowchart-A-0"><rect width="60" height="30"/>'
    '<foreignObject width="40" height="20"><div xmlns="http://www.w3.org/1999/xhtml" '
    'style="display: table-cell; white-space: nowrap;"><span class="nodeLabel">A</span></div>'
    '</foreignObject></g></g></svg>')

KEPT = (
    '<h1>Plan</h1><p>Plain text &amp; a <a href="#part-2">link inside the page</a>.</p>',
    '<style>h1{color:#333} .x{background:url(data:image/png;base64,AAAA)}</style><h1>x</h1>',
    '<img alt="chart" src="data:image/png;base64,iVBORw0KGgo=">',
    '<img src="  DATA:image/svg+xml;utf8,&lt;svg/&gt;">',
    '<svg viewBox="0 0 10 10"><defs><linearGradient id="g"/></defs>'
    '<rect fill="url(#g)" width="10" height="10"/><use href="#g"/></svg>',
    '<svg><image href="data:image/png;base64,AAAA" width="4" height="4"/></svg>',
    '<svg><animate attributeName="opacity" values="0;1" dur="1s"/></svg>',
    '<details><summary>More</summary><table><tr><td>1</td></tr></table></details>',
    MERMAID_CLI_SVG,
    # text attributes are read as text only: a backslash or image( there is no CSS
    '<img alt="C:\\x\\image(1).png" title="Z:\\w" aria-label="a\\b" src="data:image/png;base64,AA">',
    '<style>a > b{color:#333}</style><p>C:\\x &amp; image(1)</p>',
)


class CheckTest(unittest.TestCase):
    def test_each_refused_sample_says_why(self):
        for html, word in REFUSED:
            with self.subTest(html=html):
                why = pagecheck.check(html)
                self.assertIsNotNone(why)
                self.assertIn(word.lower(), why.lower())

    def test_markup_a_browser_reads_differently_is_refused(self):
        for html, word in DIFFERENTIAL:
            with self.subTest(html=html):
                why = pagecheck.check(html)
                self.assertIsNotNone(why)
                if word is not None:
                    self.assertIn(word.lower(), why.lower())

    def test_each_raw_word_is_refused_by_itself(self):
        self.assertEqual(sorted(w for _, w in RAW_ONLY),
                         sorted(set(pagecheck.RAW) - {'<script', 'javascript:', 'http-equiv'}))
        for html, word in RAW_ONLY:
            with self.subTest(html=html):
                self.assertEqual(pagecheck.check(html),
                                 'the page holds %r: a page is static HTML that runs nothing'
                                 % word)

    def test_the_kept_samples_pass(self):
        for html in KEPT:
            with self.subTest(html=html[:60]):
                self.assertIsNone(pagecheck.check(html))

    def test_mermaid_refusals_say_render_it_to_svg_first(self):
        self.assertEqual(pagecheck.MERMAID_SAYS, 'render it to svg first')
        for html, word in REFUSED:
            if word == pagecheck.MERMAID_SAYS:
                with self.subTest(html=html):
                    self.assertIn('render it to svg first', pagecheck.check(html))

    def test_the_size_cap_counts_utf8_bytes(self):
        self.assertEqual(pagecheck.MAX_BYTES, 512 * 1024)
        self.assertIsNone(pagecheck.check('x' * pagecheck.MAX_BYTES))
        self.assertIn('bytes', pagecheck.check('x' * (pagecheck.MAX_BYTES + 1)))
        self.assertIn('bytes', pagecheck.check('é' * (pagecheck.MAX_BYTES // 2 + 1)))

    def test_what_is_not_text(self):
        self.assertIn('string', pagecheck.check(None))
        self.assertIn('string', pagecheck.check(b'<p>x</p>'))
        self.assertIn('surrogate', pagecheck.check('<p>\ud800</p>'))


if __name__ == '__main__':
    unittest.main()
