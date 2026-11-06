from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from statistics import median
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, jsonify, request, send_file

app: Flask = Flask(__name__)
MAX_PAGE_BYTES = 2_000_000
MARKETS = {
	"US": ("United States", "USD"), "GB": ("United Kingdom", "GBP"),
	"CA": ("Canada", "CAD"), "AU": ("Australia", "AUD"),
	"DE": ("Germany", "EUR"), "FR": ("France", "EUR"), "JP": ("Japan", "JPY"),
}


class ProductPageParser(HTMLParser):
	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.metadata: dict[str, str] = {}
		self.jsonld: list[str] = []
		self._script_type = ""
		self._script_data: list[str] = []
		self._in_title = False
		self._title_parts: list[str] = []

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		values = {key.lower(): value or "" for key, value in attrs}
		if tag == "meta":
			key = values.get("property") or values.get("name") or values.get("itemprop")
			content = values.get("content")
			if key and content:
				self.metadata[key.casefold()] = content.strip()
		elif tag == "script" and values.get("type", "").split(";", 1)[0].strip().casefold() == "application/ld+json":
			self._script_type = "application/ld+json"
			self._script_data = []
		elif tag == "title":
			self._in_title = True

	def handle_endtag(self, tag: str) -> None:
		if tag == "script" and self._script_type:
			self.jsonld.append("".join(self._script_data))
			self._script_type = ""
			self._script_data = []
		elif tag == "title":
			self._in_title = False

	def handle_data(self, data: str) -> None:
		if self._script_type:
			self._script_data.append(data)
		if self._in_title:
			self._title_parts.append(data)


def _objects(value: object):
	if isinstance(value, dict):
		yield value
		if value.get("@graph") is not None:
			yield from _objects(value["@graph"])
	elif isinstance(value, list):
		for item in value:
			yield from _objects(item)


def _number(value: object) -> float | None:
	if isinstance(value, (int, float, Decimal)):
		result = float(value)
		return result if result > 0 else None
	if not isinstance(value, str):
		return None
	match = re.search(r"\d[\d\s.,'’]*", value)
	if not match:
		return None
	cleaned = re.sub(r"[\s'’]", "", match.group())
	if "," in cleaned and "." in cleaned:
		decimal_mark = "." if cleaned.rfind(".") > cleaned.rfind(",") else ","
		thousands_mark = "," if decimal_mark == "." else "."
		cleaned = cleaned.replace(thousands_mark, "").replace(decimal_mark, ".")
	elif "," in cleaned:
		tail = cleaned.rsplit(",", 1)[1]
		cleaned = cleaned.replace(",", ".") if len(tail) in (1, 2) else cleaned.replace(",", "")
	elif cleaned.count(".") > 1:
		head, tail = cleaned.rsplit(".", 1)
		cleaned = head.replace(".", "") + ("." + tail if len(tail) in (1, 2) else tail)
	try:
		result = float(Decimal(cleaned))
	except InvalidOperation:
		return None
	return result if result > 0 else None


def _product_details(parser: ProductPageParser, page_url: str) -> dict[str, object]:
	meta = parser.metadata
	product: dict[str, object] = {}
	for script in parser.jsonld:
		try:
			parsed = json.loads(script)
		except (TypeError, json.JSONDecodeError):
			continue
		for item in _objects(parsed):
			kind = item.get("@type", [])
			kinds = [kind] if isinstance(kind, str) else kind
			if any(str(value).rsplit("/", 1)[-1].casefold() in {"product", "productgroup"} for value in kinds):
				product = item
				break
		if product:
			break

	title = str(product.get("name") or meta.get("og:title") or meta.get("twitter:title") or "".join(parser._title_parts)).strip()
	brand = product.get("brand")
	if isinstance(brand, dict):
		brand = brand.get("name")
	if brand and str(brand).casefold() not in title.casefold():
		title = f"{brand} {title}"
	image = product.get("image") or meta.get("og:image") or meta.get("twitter:image")
	if isinstance(image, list):
		image = image[0] if image else None
	if isinstance(image, dict):
		image = image.get("url")

	offers = product.get("offers", [])
	if isinstance(offers, dict):
		offers = [offers]
	if not isinstance(offers, list):
		offers = []
	price = currency = None
	for offer in offers:
		if isinstance(offer, dict):
			price = _number(offer.get("price") or offer.get("lowPrice"))
			currency = offer.get("priceCurrency")
			if price:
				break
	price = price or _number(meta.get("product:price:amount") or meta.get("og:price:amount") or meta.get("price"))
	currency = currency or meta.get("product:price:currency") or meta.get("og:price:currency") or meta.get("pricecurrency")
	return {
		"title": title, "price": price, "currency": str(currency).upper() if currency else None,
		"image": urljoin(page_url, str(image)) if image else None,
	}


def _validate_public_url(value: str) -> str:
	if len(value) > 2048:
		raise ValueError("That product URL is too long.")
	parsed = urlparse(value)
	if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
		raise ValueError("Enter a complete product URL beginning with http:// or https://.")
	try:
		port = parsed.port
	except ValueError as error:
		raise ValueError("Enter a valid product URL.") from error
	hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
	if hostname.casefold() in {"localhost", "localhost.localdomain"} or hostname.casefold().endswith(".local"):
		raise ValueError("That address cannot be checked.")
	try:
		addresses = {item[4][0] for item in socket.getaddrinfo(hostname, port or (443 if parsed.scheme == "https" else 80))}
	except OSError as error:
		raise ValueError("The product website could not be reached.") from error
	if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
		raise ValueError("That address cannot be checked.")
	netloc = (f"[{hostname}]" if ":" in hostname else hostname) + (f":{port}" if port else "")
	return parsed._replace(netloc=netloc).geturl()


def _fetch_product(url: str) -> tuple[dict[str, object], str]:
	session = requests.Session()
	headers = {"User-Agent": "Mozilla/5.0 (compatible; PriceCheck/1.0)", "Accept": "text/html,application/xhtml+xml"}
	current_url = url
	for _ in range(6):
		current_url = _validate_public_url(current_url)
		response = session.get(current_url, headers=headers, timeout=(5, 12), stream=True, allow_redirects=False)
		if response.is_redirect or response.is_permanent_redirect:
			location = response.headers.get("Location")
			response.close()
			if not location:
				raise ValueError("The product website returned an invalid redirect.")
			current_url = urljoin(current_url, location)
			continue
		if response.status_code >= 400:
			response.close()
			raise ValueError(f"The product website returned an error ({response.status_code}).")
		content_type = response.headers.get("Content-Type", "").casefold()
		if content_type and "html" not in content_type:
			response.close()
			raise ValueError("That link did not return a product webpage.")
		chunks: list[bytes] = []
		size = 0
		for chunk in response.iter_content(32_768):
			size += len(chunk)
			if size > MAX_PAGE_BYTES:
				response.close()
				raise ValueError("The product webpage is too large to inspect.")
			chunks.append(chunk)
		encoding = response.encoding or "utf-8"
		response.close()
		parser = ProductPageParser()
		parser.feed(b"".join(chunks).decode(encoding, errors="replace"))
		details = _product_details(parser, current_url)
		if not details["title"]:
			raise ValueError("Could not identify a product name on that page. Try a product page with a readable title.")
		return details, current_url
	raise ValueError("The product website redirected too many times.")


STOP_WORDS = {"with", "and", "for", "the", "new", "from", "inch", "inches", "cm", "mm"}
CONDITION_PATTERN = re.compile(r"\b(?:refurbished|renewed|used|pre[\s-]?owned|open[\s-]?box)\b", re.IGNORECASE)


def _tokens(value: str) -> set[str]:
	return {token for token in re.findall(r"[a-z0-9]+", value.casefold()) if token not in STOP_WORDS}


def _matching_results(title: str, results: object) -> list[dict[str, object]]:
	if not isinstance(results, list):
		return []
	target = _tokens(title)
	identifiers = {token for token in target if any(char.isdigit() for char in token) and len(token) >= 3}
	matches: list[dict[str, object]] = []
	seen: set[str] = set()
	for result in results:
		if not isinstance(result, dict):
			continue
		candidate_title = str(result.get("title") or "").strip()
		candidate_tokens = _tokens(candidate_title)
		if not candidate_tokens or CONDITION_PATTERN.search(candidate_title):
			continue
		if identifiers and not identifiers.issubset(candidate_tokens):
			continue
		if len(target & candidate_tokens) / max(1, len(target)) < 0.62:
			continue
		price = _number(result.get("extracted_price")) or _number(result.get("price", ""))
		link = str(result.get("link") or "")
		if not price or urlparse(link).scheme not in {"http", "https"}:
			continue
		identity = link.split("?", 1)[0].casefold() or candidate_title.casefold()
		if identity in seen:
			continue
		seen.add(identity)
		image = str(result.get("thumbnail") or "")
		matches.append({
			"title": candidate_title, "price": price, "seller": str(result.get("source") or "Online retailer"),
			"link": link, "image": image if urlparse(image).scheme in {"http", "https"} else None,
		})
	return sorted(matches, key=lambda item: float(item["price"]))[:30]


PAGE = r'''<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Goodbuy | Price check</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap" rel="stylesheet">
  <style>
	:root{color-scheme:light;--ink:#192724;--muted:#71807b;--line:#dce4df;--paper:#f3f6f3;--white:#fff;--green:#126b56;--green-pale:#e0f0e9;--coral:#de654c;--mono:'DM Mono',monospace;--sans:'DM Sans',sans-serif;--display:'Manrope',sans-serif}*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--ink);background-color:var(--paper);background-image:linear-gradient(rgba(18,107,86,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(18,107,86,.035) 1px,transparent 1px);background-size:36px 36px;font-family:var(--sans)}button,input,select{font:inherit}.shell{width:min(1180px,calc(100% - 48px));margin:0 auto}header{height:76px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:11px;font:800 19px var(--display)}.brand-mark{width:30px;height:30px;border-radius:9px;background:var(--green);display:grid;place-items:center;color:white}.brand-mark svg{width:17px;height:17px}.header-right{display:flex;align-items:center;gap:9px;color:var(--muted);font:11px var(--mono);text-transform:uppercase;letter-spacing:.08em}.live-dot{width:7px;height:7px;border-radius:50%;background:#54a778;box-shadow:0 0 0 3px #54a77822}main{padding:55px 0 70px}.eyebrow{color:var(--green);font:500 11px var(--mono);letter-spacing:.1em;text-transform:uppercase}h1{margin:12px 0 9px;font:700 clamp(34px,4vw,49px)/1.08 var(--display)}.intro{margin:0;color:#66736f;font-size:15px}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:20px;margin-top:34px;align-items:start}.panel{background:var(--white);border:1px solid var(--line);border-radius:8px}.input-panel{padding:25px 27px 22px}.panel-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:19px}h2{margin:0;font:700 17px var(--display)}.step{color:var(--muted);font:11px var(--mono)}label{display:block;margin:16px 0 7px;color:#46544f;font-size:12px;font-weight:600}.url-wrap{position:relative}.url-icon{position:absolute;left:14px;top:50%;transform:translateY(-50%);width:17px;height:17px;color:#8a9892;pointer-events:none}input,select{width:100%;height:46px;padding:0 13px;border:1px solid #d7dfda;border-radius:5px;background:#fff;color:var(--ink);outline:none;transition:border-color .16s,box-shadow .16s}input:focus,select:focus{border-color:var(--green);box-shadow:0 0 0 3px #126b5617}input::placeholder{color:#a1aaa6}#productUrl{padding-left:41px}.form-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}.subline{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px;color:var(--muted);font-size:11px}.privacy{display:flex;align-items:center;gap:6px}.privacy svg{width:13px;height:13px;flex:none}details{margin-top:19px;border-top:1px solid #edf0ed;padding-top:14px}summary{color:#586760;font-size:12px;font-weight:600;cursor:pointer;list-style:none;display:flex;align-items:center;justify-content:space-between}summary::-webkit-details-marker{display:none}summary::after{content:'+';font:17px var(--mono);color:var(--green)}details[open] summary::after{content:'−'}.key-field{margin-top:6px}.key-help{color:var(--muted);font-size:11px;margin:7px 0 0;line-height:1.5}.key-help a{color:var(--green)}.submit-row{display:flex;align-items:center;gap:13px;margin-top:22px}.submit{min-width:174px;height:46px;padding:0 19px;border:0;border-radius:5px;background:var(--green);color:#fff;font-weight:700;font-size:13px;cursor:pointer;transition:background .15s,transform .15s}.submit:hover{background:#0b5846;transform:translateY(-1px)}.submit:disabled{opacity:.65;cursor:wait;transform:none}.submit svg{width:15px;height:15px;vertical-align:-3px;margin-left:6px}.submit-note{color:#89958f;font-size:11px}.status{margin-top:15px;padding:11px 13px;border-radius:5px;font-size:12px;line-height:1.5;display:none}.status.error{display:block;color:#8e3929;background:#fff0ec;border:1px solid #f2d5ce}.status.info{display:block;color:#315d50;background:#eaf4ef;border:1px solid #d5e8dd}.results{min-height:365px;overflow:hidden}.result-top{padding:20px 21px 17px;border-bottom:1px solid #e7ece8}.result-caption{display:flex;align-items:center;justify-content:space-between;color:var(--muted);font:10px var(--mono);text-transform:uppercase;letter-spacing:.09em}.check-mark{width:21px;height:21px;border-radius:50%;display:grid;place-items:center;background:var(--green-pale);color:var(--green)}.check-mark svg{width:12px;height:12px}.verdict{margin:14px 0 5px;font:700 21px var(--display)}.verdict.good{color:var(--green)}.verdict.high{color:#bd533d}.verdict.neutral{color:var(--ink)}.verdict-sub{margin:0;color:var(--muted);font-size:12px;line-height:1.45}.metrics{display:grid;grid-template-columns:1fr 1fr;padding:17px 21px 19px;gap:12px;border-bottom:1px solid #e7ece8}.metric-label{display:block;margin-bottom:6px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.07em}.metric-value{font:700 21px var(--display)}.metric-delta{color:var(--green);font:500 11px var(--mono);margin-top:5px}.metric-delta.high{color:#bd533d}.sample-line{display:flex;align-items:center;justify-content:space-between;padding:14px 21px;color:var(--muted);font-size:11px}.sample-line strong{color:var(--ink);font:500 11px var(--mono)}.empty-state{min-height:365px;padding:26px;display:flex;flex-direction:column;justify-content:center;align-items:flex-start;position:relative;overflow:hidden}.empty-index{color:#98aaa1;font:11px var(--mono)}.empty-shape{width:51px;height:51px;margin:22px 0 17px;background:var(--green-pale);border-radius:13px;display:grid;place-items:center;color:var(--green)}.empty-shape svg{width:26px;height:26px}.empty-title{margin:0 0 7px;font:700 17px var(--display)}.empty-copy{margin:0;color:var(--muted);font-size:12px;line-height:1.6;max-width:225px}.empty-lines{position:absolute;right:-19px;bottom:-16px;color:#dfece5;transform:rotate(-14deg)}.empty-lines svg{width:150px;height:150px}.offer-section{margin-top:23px;display:none}.offer-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:11px}.offer-count{color:var(--muted);font:11px var(--mono)}.offer-list{border-top:1px solid var(--line)}.offer{min-height:76px;display:grid;grid-template-columns:48px minmax(0,1fr) auto 28px;align-items:center;gap:14px;border-bottom:1px solid var(--line);padding:10px 8px}.offer-image{width:46px;height:46px;border:1px solid #e6ebe7;border-radius:5px;object-fit:contain;background:white}.offer-placeholder{display:grid;place-items:center;color:#809189;background:#edf2ee}.offer-placeholder svg{width:20px;height:20px}.offer-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink);font-size:12px;font-weight:600;text-decoration:none}.offer-name:hover{color:var(--green)}.offer-seller{display:block;margin-top:5px;color:var(--muted);font-size:11px}.offer-price{font:600 13px var(--mono);white-space:nowrap}.offer-out{display:grid;place-items:center;width:27px;height:27px;border:1px solid var(--line);border-radius:4px;color:#6a7973}.offer-out:hover{border-color:var(--green);color:var(--green)}.offer-out svg{width:13px;height:13px}.fine-print{margin-top:17px;color:#89958f;font-size:10px;line-height:1.5}footer{border-top:1px solid var(--line);padding:17px 0 25px;color:#87938d;font:10px var(--mono);display:flex;justify-content:space-between;gap:12px}.hidden{display:none!important}@media(max-width:780px){.shell{width:min(100% - 32px,580px)}main{padding:39px 0 48px}.workspace{grid-template-columns:1fr;gap:15px}.results{min-height:unset}.empty-state{min-height:260px}.form-row{grid-template-columns:1fr 1fr}.offer-section{margin-top:18px}}@media(max-width:480px){header{height:64px}.header-right{font-size:9px}.form-row{grid-template-columns:1fr;gap:0}.input-panel{padding:20px 17px}.offer{grid-template-columns:40px minmax(0,1fr) auto 26px;gap:9px}.offer-image{width:40px;height:40px}.submit-row{align-items:flex-start;flex-direction:column}.submit{width:100%}.metrics{padding-left:17px;padding-right:17px}.result-top,.sample-line{padding-left:17px;padding-right:17px}}
  </style>
	<style>
		.product-name{padding:12px 21px;border-bottom:1px solid #e7ece8;color:#52635c;font-size:12px;font-weight:600;line-height:1.45}
		@media(max-width:480px){.product-name{padding-left:17px;padding-right:17px}}
	</style>
</head><body><div class="shell">
  <header><div class="brand"><span class="brand-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5z"/><path d="m8 12 2.5 2.5L16 9"/></svg></span>goodbuy</div><div class="header-right"><span class="live-dot"></span> independent price check</div></header>
  <main><div class="eyebrow">A clearer read on the asking price</div><h1>Is it worth it?</h1><p class="intro">Check a product against current listings across the web.</p>
	<div class="workspace">
	  <section class="panel input-panel"><div class="panel-heading"><h2>Check a listing</h2><span class="step">01 / PRODUCT</span></div>
		<form id="checkForm"><label for="productUrl">Product page URL</label><div class="url-wrap"><svg class="url-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg><input id="productUrl" name="product_url" type="url" placeholder="https://store.com/product/..." autocomplete="url" required></div>
		  <div class="form-row"><div><label for="market">Shopping market</label><select id="market" name="market"><option value="US">United States · USD</option><option value="GB">United Kingdom · GBP</option><option value="CA">Canada · CAD</option><option value="AU">Australia · AUD</option><option value="DE">Germany · EUR</option><option value="FR">France · EUR</option><option value="JP">Japan · JPY</option></select></div><div><label for="manualPrice">Listing price <span style="font-weight:400;color:#8b9892">· optional override</span></label><input id="manualPrice" name="manual_price" inputmode="decimal" placeholder="Read from product page"></div></div>
		  <div class="subline"><span>Page price is used when available.</span><span class="privacy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 1 1 8 0v3m-4 4v3"/></svg>Nothing is saved</span></div>
		  <details><summary>Shopping search access</summary><div class="key-field"><label for="apiKey">SerpApi key</label><input id="apiKey" name="api_key" type="password" autocomplete="off" placeholder="Paste your key for this check"><p class="key-help">Used only for this request and not stored. <a href="https://serpapi.com/manage-api-key" target="_blank" rel="noreferrer">Get a SerpApi key</a></p></div></details>
		  <div class="submit-row"><button class="submit" id="submitButton" type="submit">Compare prices <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14m-7-7 7 7-7 7"/></svg></button><span class="submit-note">Searches current public listings</span></div><div class="status" id="status" role="status" aria-live="polite"></div>
		</form>
	  </section>
	  <section class="panel results" aria-live="polite"><div class="empty-state" id="emptyState"><span class="empty-index">MARKET READ / —</span><span class="empty-shape"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m7 14 4-4 4 4 6-7"/><path d="M17 7h4v4"/></svg></span><h2 class="empty-title">Your comparison, at a glance.</h2><p class="empty-copy">A price verdict and a short list of comparable offers will appear here.</p><div class="empty-lines"><svg viewBox="0 0 150 150" fill="none" stroke="currentColor"><path d="M20 130h130M20 110h130M20 90h130M20 70h130M20 50h130M20 30h130M20 10h130M30 0v150M50 0v150M70 0v150M90 0v150M110 0v150M130 0v150"/></svg></div></div>
		<div id="resultContent" class="hidden"><div class="product-name" id="productTitle"></div><div class="result-top"><div class="result-caption"><span>Market read</span><span class="check-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4L19 6"/></svg></span></div><h2 class="verdict" id="verdict"></h2><p class="verdict-sub" id="verdictSub"></p></div><div class="metrics"><div><span class="metric-label">This listing</span><strong class="metric-value" id="currentPrice"></strong></div><div><span class="metric-label">Market median</span><strong class="metric-value" id="medianPrice"></strong><div class="metric-delta" id="priceDelta"></div></div></div><div class="sample-line"><span>Comparable offers</span><strong id="sampleCount"></strong></div></div>
	  </section>
	</div>
	<section class="offer-section" id="offerSection"><div class="offer-heading"><h2>Comparable listings</h2><span class="offer-count" id="offerCount"></span></div><div class="offer-list" id="offerList"></div><p class="fine-print">Prices are a live snapshot of public listings. Availability, shipping, seller reputation, product configuration and condition can differ; confirm details with the retailer before buying.</p></section>
  </main><footer><span>GOODBUY / PRICE INTELLIGENCE</span><span>Public listings · indicative comparison</span></footer>
</div>
<script>
const form=document.querySelector('#checkForm'),statusBox=document.querySelector('#status'),button=document.querySelector('#submitButton');
function money(amount,currency){try{return new Intl.NumberFormat(undefined,{style:'currency',currency,maximumFractionDigits:2}).format(amount)}catch{return `${currency} ${Number(amount).toFixed(2)}`}}
function setStatus(message,kind){statusBox.textContent=message;statusBox.className=`status ${kind}`}
function placeholderImage(){const span=document.createElement('span');span.className='offer-image offer-placeholder';span.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m21 15-5-5L5 20"/></svg>';return span}
function renderOffers(offers,currency){const list=document.querySelector('#offerList');list.replaceChildren();for(const offer of offers){const row=document.createElement('article');row.className='offer';if(offer.image){const img=document.createElement('img');img.className='offer-image';img.src=offer.image;img.alt='';img.loading='lazy';img.referrerPolicy='no-referrer';img.onerror=()=>img.replaceWith(placeholderImage());row.append(img)}else row.append(placeholderImage());const text=document.createElement('div'),name=document.createElement('a');name.className='offer-name';name.href=offer.link;name.target='_blank';name.rel='noopener noreferrer';name.textContent=offer.title;name.title=offer.title;const seller=document.createElement('span');seller.className='offer-seller';seller.textContent=offer.seller;text.append(name,seller);row.append(text);const price=document.createElement('strong');price.className='offer-price';price.textContent=money(offer.price,currency);row.append(price);const out=document.createElement('a');out.className='offer-out';out.href=offer.link;out.target='_blank';out.rel='noopener noreferrer';out.setAttribute('aria-label',`View offer from ${offer.seller}`);out.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3h7v7m-1-6-9 9"/><path d="M19 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h6"/></svg>';row.append(out);list.append(row)}document.querySelector('#offerCount').textContent=`${offers.length} shown`;document.querySelector('#offerSection').style.display=offers.length?'block':'none'}
function renderResult(data){
	document.querySelector('#productTitle').textContent=data.title;
	document.querySelector('#emptyState').classList.add('hidden');
	document.querySelector('#resultContent').classList.remove('hidden');
	const verdict=document.querySelector('#verdict');
	verdict.textContent=data.verdict;
	verdict.className=`verdict ${data.tone}`;
	document.querySelector('#verdictSub').textContent=data.summary;
	document.querySelector('#currentPrice').textContent=money(data.current_price,data.currency);
	document.querySelector('#medianPrice').textContent=data.median_price==null?'—':money(data.median_price,data.currency);
	const delta=document.querySelector('#priceDelta');
	if(data.delta_percent==null){delta.textContent='Not enough matches';delta.className='metric-delta'}
	else{const lower=data.delta_percent<=0;delta.textContent=`${Math.abs(data.delta_percent).toFixed(1)}% ${lower?'below':'above'} median`;delta.className=`metric-delta ${lower?'':'high'}`}
	document.querySelector('#sampleCount').textContent=`${data.sample_count} · ${data.confidence}`;
	renderOffers(data.offers,data.currency);
}
form.addEventListener('submit',async event=>{event.preventDefault();statusBox.className='status';statusBox.textContent='';button.disabled=true;button.textContent='Checking listings…';const payload=Object.fromEntries(new FormData(form));try{const response=await fetch('/api/compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),data=await response.json();if(!response.ok)throw new Error(data.error||'The comparison could not be completed.');renderResult(data);if(data.sample_count<3)setStatus('Not enough strong matches for a price verdict. Try a more specific product URL or enter the exact model name.','info')}catch(error){setStatus(error.message||'The comparison could not be completed.','error')}finally{button.disabled=false;button.innerHTML='Compare prices <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14m-7-7 7 7-7 7"/></svg>'}});
</script>
<script>
form.addEventListener('submit',event=>{
	const key=document.querySelector('#apiKey');
	if(!key.value.trim()){
		event.preventDefault();
		event.stopImmediatePropagation();
		key.closest('details').open=true;
		key.focus();
		setStatus('Enter a SerpApi key to search current listings. Use the link below to get one.','error');
	}
},true);
</script></body></html>'''


@app.get("/")
def home():
	return send_file(os.path.join(os.path.dirname(__file__), "public", "index.html"))


@app.post("/api/compare")
def compare():
	payload = request.get_json(silent=True) or {}
	product_url = str(payload.get("product_url", "")).strip()
	market = str(payload.get("market", "US")).upper()
	api_key = str(payload.get("api_key", "")).strip()
	if market not in MARKETS:
		return jsonify(error="Choose a supported shopping market."), 400
	if not api_key:
		return jsonify(error="Add a SerpApi key under Shopping search access to search current listings."), 400
	if len(api_key) > 256:
		return jsonify(error="The shopping search key is invalid."), 400
	try:
		product, page_url = _fetch_product(product_url)
	except ValueError as error:
		return jsonify(error=str(error)), 400
	except requests.RequestException:
		return jsonify(error="Could not read that product page. The store may block automated requests."), 502

	currency = MARKETS[market][1]
	if product.get("currency") and product["currency"] != currency:
		return jsonify(error=f"This listing is priced in {product['currency']}. Choose a shopping market that uses {product['currency']} so prices can be compared fairly."), 400
	current_price = _number(payload.get("manual_price", "")) or product.get("price")
	if not current_price:
		return jsonify(error="No price was found on that page. Enter the listing price to continue."), 400
	try:
		response = requests.get(
			"https://serpapi.com/search.json",
			params={"engine": "google_shopping", "q": str(product["title"])[:300], "gl": market.lower(), "hl": "en", "api_key": api_key},
			timeout=(5, 25),
		)
		if response.status_code in {401, 403}:
			return jsonify(error="Shopping search rejected that key. Check your SerpApi key and account access."), 400
		response.raise_for_status()
		search_data = response.json()
	except requests.Timeout:
		return jsonify(error="Shopping search timed out. Please try again."), 504
	except requests.RequestException:
		return jsonify(error="Shopping search is temporarily unavailable. Please try again."), 502
	except ValueError:
		return jsonify(error="Shopping search returned an unreadable response."), 502
	if search_data.get("error"):
		return jsonify(error="Shopping search could not complete: " + str(search_data["error"])[:220]), 502

	offers = _matching_results(str(product["title"]), search_data.get("shopping_results"))
	sample_count = len(offers)
	median_price = float(median(float(offer["price"]) for offer in offers)) if offers else None
	delta_percent = ((float(current_price) / median_price) - 1) * 100 if median_price else None
	verdict, tone = "Not enough matches", "neutral"
	if sample_count >= 3 and delta_percent is not None:
		if delta_percent <= -15:
			verdict, tone = "Strong price", "good"
		elif delta_percent <= -5:
			verdict, tone = "Good price", "good"
		elif delta_percent <= 5:
			verdict, tone = "Around market", "neutral"
		elif delta_percent <= 15:
			verdict, tone = "A little high", "high"
		else:
			verdict, tone = "Above market", "high"
	confidence = "Strong sample" if sample_count >= 10 else "Useful sample" if sample_count >= 6 else "Early read"
	summary = (
		f"About {abs(delta_percent):.1f}% below comparable prices." if delta_percent is not None and delta_percent < 0 else
		f"About {delta_percent:.1f}% above comparable prices." if delta_percent is not None and delta_percent > 0 else
		"At the comparable-price median." if delta_percent is not None else
		"A price read needs at least three close matches."
	)
	if 3 <= sample_count < 6:
		summary += " Small sample; treat as an early read."
	return jsonify(
		title=product["title"], source_url=page_url, image=product["image"], current_price=float(current_price),
		currency=currency, median_price=median_price, delta_percent=round(delta_percent, 1) if delta_percent is not None else None,
		verdict=verdict, tone=tone, summary=summary, sample_count=sample_count, confidence=confidence, offers=offers[:12],
	)


if __name__ == "__main__":
	app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8501")), debug=False)
