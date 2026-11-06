"""Bitmaps of the fixed phrases in Brønnøysund's rendered annual-account form (generated, do not edit).

Each entry is (text, kind, width, height, base64(zlib(numpy.packbits(ink)))) where ``ink`` is the phrase's
bounding-box bitmap (True = black) at the native 1728x2312 render. All occurrences in 112 sampled filings
(2025 accounts, fetched 2026-11-06) were bit-identical. Kinds: label, title:SELSKAP, title:KONSERN.
"""

TEMPLATES: list[tuple[str, str, int, int, str]] = [
    ('NOTEOPPLYSNINGER - SELSKAP', 'title:SELSKAP', 519, 24,
     'eNrtVD1uWzkQHoLFdOQFGL1rEIiw70o0togLb0RBxeucC+QgLmmoULkXUEFBhbuEhosQMJezH2XLF0gbFpTmfTPD+flmmi1T3MhB'
     'mqpa8ty1UJ/S9NDsi9Rmiz0ONNH7mR42shdSr8t/ALWc2pQliXyTahtL2Qh31eYoz216GleWOtB8dSBFZOlkxb7WqbC8tHkvjyKT'
     'lLnZgdqm60bLuc3/jmsvzwO9RoAnhwrNffpV5gyLauxuvximXNd6m/1KR5VveUnVUKCB5mBYXx0ACEZHur1jGznr7am6tT4YZ9Q+'
     'rVnlsGJSKa/0tjrKqjoHyQ2TtwP/2UEle/MXTYnVufqg7513eiFvLsoEX5519SrpgW6Th8n78aySN6RjcJU+R6MO4Saz88F/WVFw'
     'F2XSB0LUIeiIK7NeAkzeD+JfgiNk/DnTS3T6vpwjI9mQPQVvdSye9JEKmZIZ1zkaRjHWVweFIHqyRJuoDuStez6QoUySAuWwRrUC'
     '6Qfq5J6ToYESai7l6qAPMZDN+Mff4Nmf7wl1UhKzSuACfonhL/tzdDRQmqq6dPHCBclTxXu2q07GkNSbJ6jpqAUV2wLtqBEn2uSb'
     'J0QK1La56A7TNy+bPJeh0nUjKEs+/UDujMRQMZAYyhEi2nT6gUiBThV1dBcKjVai0RkqtOLvtGl49/GIbhkyn/Zx9CqM+iJ39fh4'
     'BA2AzuV0NO5aAmR7OjKdkjNrksakdxXdcvRGA0J24MJIarerSGqgo7MfTfBDBOkivukIC9alWCTr2VJx4CBcxZEUgJFUKYyAvf+g'
     'wRDHa/iG7jsyOte7QQNjlwQGRBrfqa4B8AUFtXO4vTqoa4h4bZfCrYMXkiVJsZ0Fk4gqzVgJHS3CCC/7cSVp2Abt67s9gLEN+iRL'
     '+yr8KlvhKGmC9WUb0AYD3qY2ZpYXzDHQsQ1q+xjmjG0gbe62/tPtL9Gd6Y7mim3AyXbQBK7myjMAK2eg8xbboNTrMM8K20Dq32Uq'
     'r2362enP+f3zP+JJ5yM='),
    ('Antall årsverk i regnskapsåret', 'label', 499, 24,
     'eNqdVL1u2zAQPoKAiQKFuGZgzaEv0FEFjPBVPHXmFg9GRMODt3rtljcJaHjQUrSPYBYeNJZGBssIy+vJP4GdFkWaIySLx/v9vqMB'
     'LqSAl0n4q5ZHeFE482wvg+wCfjpTjelh/tyInYL3hodfQT7c0UFiibZvz6wlqaG8yHF9+vh6bCDT23bFONNZx2eZxIW3OAWX+7cC'
     '7Y/d2X2Q/r+bPpXC9rhR0eNjd8Uf1pdNH5Jfbi3oyTZgFIH5vXWS39lWbFrDduFj6jqiZ8Batl3dsi000YiVuw7S3454NIl/MFln'
     'kamqri5cPLZVyEa3phLkKqh78TOgx0cMVVDZREyyW6sFztA1Atd0GMS+Lp0M6qTrho5CQ64drJgMqausE7+rUeMSJ7jAtmoxXg0H'
     'SsFbRQQQFmzqEq/dcBhG2r0Lh+55FLyeTxfL2t24YWhHo3hVilCIGYgs+NQRgIoo6KDog7WFMsj9fva6fkYMd8Ha0lrpQfXF54IK'
     'jRxjAPR0ADpy180Nz9yJFpYCg4XbI84FGCyLQoiWtYBTZxFMUFBlBSapogCZD5Qz9BakVwETKcPhTjAvsGELT1gbV0VbsJYwcBKX'
     'rsenk4V/j/5AedUWUCr2pU8+7Nt+pLryI8SyVKUSpc6S15zwtjQ09o0vbTkY2PJAeSEo08wZFoZE4vEeCJBcasFnM7emXJEk2AL6'
     'PSmzRiJijjUtT6XviMDIvcmm1TTJEol2ZLjB9S5hHFdV7kZE4zxz/IHE30M3Ii3SIspX5JVgxIlyvm4pHEGPgaYgVeYuyXmzrXGK'
     'k18b9GRJ05UrEeSdpkkgiB4a5PfQA+0wjCua1NxNESR4nDcP92yTxm3jb7y+NkQBkfBSsacPBa+QpwvcIfm/wvHkxPAVuaun/x4T'
     'XpH8CSKRztS/AUzQdDc='),
    ('Tal på årsverk i rekneskapsåret', 'label', 515, 24,
     'eNqNlL1uIzcQgIcgcJPCWLYJQJiv4FJGBPNVrkq9hzQuFJGCgKiLXuCQvEhgULhCTRA9ghmocJej4UIEwuNkuCs7chIDGmB3ubOc'
     'v2+GC/AiPV8azpX4H43xAPhv9ZsO7cnaDXd5xbffTtS5Kf24VsenfA7w7v3RlvjjqPyOr2/8SULPVbGIdFTOnr+OkaKiCKIMa2Rb'
     'cVLAEKs7vtzA60RAjfUJKi9ZthjTN4oX4QTzoBgiST/Z+3+Ur0WeFAOT47N7vadH3RvPSnGIjb7I5mf5p9qnuXwK3+cR7vxWJrR3'
     'BYo4iF20eO9/CCrMbzHasv/J5JadJaxsT8tDok2Z2zSvnRvhqvsN+YcDBRddvC42UTFZFXX/iZB8L8lzdkiJWYLNrtps1ztStNmN'
     'cAVll3Vlq2oK2hUZ2pKgDWW2SR1c9dgQX14wMYki4xrCJiQL34aRAsYOl7/icrnYb/1neB/z7WP8eoJRK2xouzaPuIgDsTiFGPXE'
     'VfTDPDa4SVKaQ+yp9IwL9CV+1ICQkGJktC60XkVtuIVYEFQSPyoKEcqRuAZXVdCdoVWWCWjpP1SwUQOV5sfmFlnLNRNkGpEPwzVX'
     'p8GEsfXSM40iF58oqeDApb6TyRTpDbtqU9iylOGJeC8lDf1EXk3ZSn48tr7nebkO/eQWJrrrTVXydxzYB+i/0iLEluVs2vNe3XE0'
     'BXMZQg8f2CEORVy+Q90hLlarxVYuNunxjxj7DqYXSplkyXLqitacgKQnWlBE74pLN9aDaWesStrv6Sm3nuXKPa62GFpX5Dbuq/zr'
     'gS0FxcojZ+9csrnBVRW3iR3yGFCweeaqUzwCxmwPW1qR+PLIM5VMduFmRmx5ndQvzOFhV3EGF2DZKnNerrS5ElmtaRfn8k7u8yw/'
     'hM/e3DD5JPLzaUA4X/R5v7U35dXpfPmVhbPtBf2PUuXzE7DxTKdvCJaTl78B8fJ7dw=='),
    ('NOTEOPPLYSNINGER - KONSERN', 'title:KONSERN', 520, 24,
     'eNrt1D1upDAUAGBbFO7wBR7ja0xBMlci3UQaBdAUuUQOktKIgjIXQJFRii3jKI2l9fL2PTNhSNptl2LGMw9/gvfjqL1pahwwypCh'
     'O8wZitlY8xz1J4aovR45asVyqT90n7Yofz9irCOtZu1rlwBFe8MBzwmow8wARyNH/RVQs3aokAGkFWqPPgEaM4wH7BOAARng6E9A'
     '85/6Ang0svPHarIOjGqqUmW2EqUWPhhpc8HRUy6tO14AMXmjWg9aNw5yXpm2t2UCStq7U9JVAnLp9qZloO0sMLD/At5cmXUBdhkD'
     'WRvAdH0DCdhLW+VKEJDvpN2bjgGOfgN+2b3sIsAFiFB0QwKm8bgCGggoGCgSUGyA9+You5GBaSRghGIaRC6cmOjd3eUV1LN0opgY'
     'oCjkIK45QFGJ6RV2lA1LOXgF+FgATMCSxOxZzAI+COCo0FGsVaAAAS+geYdG+QJwtwHqqPg786L2cMfA3QVwtLf2X08wgEIGZjkA'
     '3G+BoLCpcumFCXDPwP2QIQPNd+BJYkNAkMMT3GxyUDtFj8g/RQk3DNwQQDl4W4HPhoB+FJmlHOxlT1mkPDMwchmFEgmQFqBgoOjP'
     'SFWY7NoHXEYqg6S65cAFMQuw9MEWMAyYpQ8mt3aiYyASQH2w45bQ1IkJ4E7cApoBiqZGWgHHnWgvAEUiqM6HBBjdXAF/BMUARUsG'
     '1jI6b3RrTzQ4BGhpT/ho0etZLdPYHOhQmKk8NIiPPX/YNI3x4Wsa4wNNYziZNI3Dezjh2dYuAXweXAGPZwYo+gO4pfMglIZOhASU'
     '2FojEiD9Bnix2DLQWs3HTby9AtLrABAZ6N8DiP/Xv19/AVSPEUY='),
]
