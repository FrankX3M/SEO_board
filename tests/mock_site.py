"""Мини-сайт для самопроверки сборщика: имитирует препрод с токеном и кукой."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "secret-token"

PAGE = ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>{t}</title>{h}</head>"
        "<body>{b}<p>" + "Текст страницы. " * 200 + "</p></body></html>")


class Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):  # тишина
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        Handler.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.headers.get("X-SP-Token") != TOKEN:
            return self._send(403, b"forbidden", {"Content-Type": "text/plain"})
        html = {"Content-Type": "text/html; charset=utf-8"}
        p = self.path
        base = "http://" + (self.headers.get("Host") or "127.0.0.1")
        if p == "/robots.txt":
            return self._send(200, f"User-agent: *\nSitemap: {base}/sitemap_5ka.xml\n".encode(), {"Content-Type": "text/plain"})
        if p == "/sitemap_5ka.xml":
            xml = ("<?xml version='1.0' encoding='UTF-8'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                   f"<sitemap><loc>{base}/sitemap_5ka_pages.xml</loc></sitemap>"
                   f"<sitemap><loc>{base}/sitemap_5ka_tags.xml</loc></sitemap></sitemapindex>")
            return self._send(200, xml.encode(), {"Content-Type": "application/xml"})
        if p == "/sitemap_5ka_pages.xml":
            xml = ("<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                   f"<url><loc>{base}/catalog/</loc><lastmod>2026-10-01</lastmod></url>"
                   f"<url><loc>{base}/ssr/</loc></url></urlset>")
            return self._send(200, xml.encode(), {"Content-Type": "application/xml"})
        if p == "/custom/map.xml":  # карта, о которой нет упоминания в robots.txt (задаётся в настройках сети)
            xml = ("<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                   f"<url><loc>{base}/hidden/</loc></url></urlset>")
            return self._send(200, xml.encode(), {"Content-Type": "application/xml"})
        if p == "/favicon.ico":
            return self._send(200, b"\x00\x00\x01\x00\x01\x00\x10\x10", {"Content-Type": "image/x-icon"})
        if p == "/manifest.json":
            data = '{"name":"Мок","icons":[{"src":"/icon-192.png"},{"src":"/icon-512.png"}]}'
            return self._send(200, data.encode(), {"Content-Type": "application/manifest+json"})
        if p == "/icon-192.png":
            png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (192).to_bytes(4, "big") * 2 + b"\x08\x06\x00\x00\x00"
            return self._send(200, png, {"Content-Type": "image/png"})
        if p == "/og.png":
            png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1200).to_bytes(4, "big") + (630).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
            return self._send(200, png, {"Content-Type": "image/png"})
        if p == "/catalog":  # слеш: правильный редирект
            return self._send(301, b"", {"Location": "/catalog/"})
        if p == "/CATALOG/":  # заглавные: редирект не тем кодом
            return self._send(302, b"", {"Location": "/catalog/"})
        if p == "/index.php":
            return self._send(301, b"", {"Location": "/"})
        if p == "/index.html":  # дубль без редиректа
            return self._send(200, PAGE.format(t="Главная", h="", b="<h1>Главная</h1>").encode(), html)
        if p.startswith("/catalog/ta-404-"):  # 404 через редирект на отдельную страницу
            return self._send(302, b"", {"Location": "/404-page/"})
        if p == "/":
            head = ("<link rel='icon' href='/favicon.ico'><link rel='manifest' href='/manifest.json'>"
                    "<meta property='og:image' content='/og.png'>")
            body = PAGE.format(t="Главная", h=head, b="<h1>Главная</h1>")
        elif p == "/catalog/":  # h1 появляется только после JS
            body = PAGE.format(t="Каталог", h="", b="<div id=app></div><script>var h=document.createElement('h1');h.textContent='Каталог';document.getElementById('app').appendChild(h);</script>")
        elif p == "/old/":
            return self._send(301, b"", {"Location": "/catalog/"})
        elif p == "/Upper/":
            return self._send(302, b"", {"Location": "/404-page/"})
        elif p == "/bot-only/":  # полноценный ответ только поисковому боту
            if "Googlebot" in (self.headers.get("User-Agent") or ""):
                body = PAGE.format(t="Бот", h="", b="<h1>Для бота</h1>")
            else:
                return self._send(200, b"<html><body>captcha</body></html>", html)
        elif p == "/ssr/":  # рендеринг только для бота; отзывы дорисовывает JS
            if "Googlebot" in (self.headers.get("User-Agent") or ""):
                cards = "".join(f"<div data-qa='product-card-{1000 + i}'><a href='/product/{i}/'>Товар {i}</a>"
                                f"<span>Цена {i}00 руб.</span></div>" for i in range(1, 4))
                body = PAGE.format(
                    t="SSR", h="", b="<header data-qa='header'><a href='/'>Логотип</a></header><h1>Каталог молока</h1>"
                               f"<div data-qa='product-list'>{cards}</div><div id='r'></div>"
                               "<footer><a href='/about/'>О компании</a></footer>"
                               "<script>var d=document.createElement('div');d.setAttribute('data-qa','reviews-block');"
                               "d.textContent='Отзывы покупателей';document.getElementById('r').appendChild(d);</script>")
            else:  # «оболочка» без контента
                body = ("<!doctype html><html><head><title>SSR</title></head><body>"
                        "<header data-qa='header'><a href='/'>Логотип</a></header><div id='__next'></div>"
                        + "<!-- пусто -->" * 50 + "</body></html>")
        elif p.startswith("/gone"):
            return self._send(404, PAGE.format(t="404", h="", b="<h1>Нет такой</h1>").encode(), html)
        else:
            return self._send(404, b"nf", html)
        self._send(200, body.encode(), html)


def start() -> tuple[ThreadingHTTPServer, str]:
    Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
