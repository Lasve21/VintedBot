#!/usr/bin/env python3
import os
import re
import time
import json
import requests
import threading
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dotenv import load_dotenv
from flask import Flask, jsonify

# Carica .env in locale (Render userà variabili d'ambiente)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
VINTED_URL = os.getenv("VINTED_URL", "")
SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notifier/1.0)"}
BASE_URL = "https://www.vinted.it"

# Flask app (esposta come web service)
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "vinted-notifier"}), 200

@app.route("/healthz", methods=["GET"])
def health():
    return "OK", 200

# --- funzioni di scraping / notifica ---
def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen(s):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(s), f)
    except Exception as e:
        print("Errore salvataggio seen:", e)

def fetch_page(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text

def parse_listings(html):
    soup = BeautifulSoup(html, "lxml")
    anchors = soup.find_all("a", href=re.compile(r"/item/\d+"))
    items = {}
    for a in anchors:
        href = a.get("href", "")
        m = re.search(r"/item/(\d+)", href)
        if not m:
            continue
        item_id = m.group(1)
        if item_id in items:
            continue
        url = urljoin(BASE_URL, href)
        img_tag = a.find("img")
        img_url = None
        title = None
        if img_tag:
            img_url = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-lazy-src")
            title = img_tag.get("alt") or title
        container = a.find_parent()
        text_search = container.get_text(" ", strip=True) if container else a.get_text(" ", strip=True)
        price_match = re.search(r"€\s?[\d\.,]+|[\d\.,]+\s?€", text_search)
        price = price_match.group(0) if price_match else ""
        items[item_id] = {"id": item_id, "url": url, "img": img_url, "title": (title or "").strip(), "price": price}
    return list(items.values())

def send_photo_by_url(token, chat_id, photo_url, caption):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=15)
        return r.ok, r.text
    except Exception as e:
        return False, str(e)

def send_photo_upload(token, chat_id, photo_bytes, caption):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    files = {"photo": ("image.jpg", photo_bytes)}
    try:
        r = requests.post(url, data=data, files=files, timeout=30)
        return r.ok, r.text
    except Exception as e:
        return False, str(e)

def notify_item(item):
    caption = f"<b>{item['title'] or 'Nuovo annuncio'}</b>\n{item['price']}\n{item['url']}"
    if item.get("img"):
        ok, resp = send_photo_by_url(TELEGRAM_TOKEN, CHAT_ID, item["img"], caption)
        if not ok:
            try:
                r = requests.get(item["img"], headers=HEADERS, timeout=15)
                if r.ok:
                    ok2, resp2 = send_photo_upload(TELEGRAM_TOKEN, CHAT_ID, r.content, caption)
                    return ok2, resp2
                else:
                    r2 = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                       json={"chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML"})
                    return r2.ok, r2.text
            except Exception as e:
                return False, str(e)
        return ok, resp
    else:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML"})
        return r.ok, r.text

# --- loop principale che gira in background ---
def background_loop():
    if not TELEGRAM_TOKEN or not CHAT_ID or not VINTED_URL:
        print("Errore: imposta TELEGRAM_TOKEN, CHAT_ID e
