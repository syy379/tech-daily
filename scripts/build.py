#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""科技日报静态页生成器：抓取多源 RSS -> 去重排序分类 -> 输出 public/index.html"""

import html
import json
import os
import re
import sys
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

import feedparser

BJ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FEEDS = [
    {"name": "量子位", "url": "https://www.qbitai.com/feed", "color": "#8b5cf6"},
    {"name": "IT之家", "url": "https://www.ithome.com/rss/", "color": "#ff6b5b"},
    {"name": "少数派", "url": "https://sspai.com/feed", "color": "#ff4d6d"},
    {"name": "极客公园", "url": "https://www.geekpark.net/rss", "color": "#06b6d4"},
    {"name": "钛媒体", "url": "https://www.tmtpost.com/rss", "color": "#3b82f6"},
    {"name": "InfoQ", "url": "https://www.infoq.cn/feed", "color": "#14b8a6"},
    {"name": "开源中国", "url": "https://www.oschina.net/news/rss", "color": "#22c55e"},
    {"name": "Solidot", "url": "https://www.solidot.org/index.rss", "color": "#84cc16"},
    {"name": "爱范儿", "url": "https://www.ifanr.com/feed", "color": "#f59e0b"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "color": "#0ea5e9"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "color": "#a855f7"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "color": "#ef4444"},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "color": "#64748b"},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "color": "#fb923c"},
]

CATS = [
    ("ai", "AI"),
    ("hardware", "硬件产品"),
    ("business", "互联网商业"),
    ("dev", "开发开源"),
    ("other", "其他"),
]

RULES = [
    ("ai", ["ai", "人工智能", "大模型", "llm", "gpt", "openai", "anthropic", "claude",
            "gemini", "deepmind", "智能体", "agent", "生成式", "机器学习", "深度学习",
            "多模态", "算力", "推理模型", "训练模型", "chatgpt", "midjourney", "stable diffusion",
            "copilot", "语言模型", "神经网络", "具身智能"]),
    ("hardware", ["苹果", "iphone", "ipad", "macbook", "airpods", "华为", "小米", "oppo",
                  "vivo", "荣耀", "三星", "芯片", "处理器", "骁龙", "台积电", "显卡", "gpu",
                  "cpu", "手机", "笔记本", "平板", "耳机", "评测", "开箱", "特斯拉", "汽车",
                  "机器人", "无人机", "vision pro", "pixel", "相机", "屏幕", "电池", "充电",
                  "英伟达", "nvidia", "amd", "intel", "高通"]),
    ("business", ["融资", "上市", "收购", "并购", "财报", "营收", "股价", "字节", "腾讯",
                  "阿里", "美团", "拼多多", "京东", "百度", "滴滴", "小红书", "估值", "ipo",
                  "裁员", "监管", "反垄断", "电商", "直播带货", "订阅", "广告", "市值",
                  "创业", "投资人", "市场份额"]),
    ("dev", ["github", "开源", "linux", "编程", "开发者", "程序员", "python", "rust",
             "javascript", "typescript", "框架", "api", "数据库", "docker", "kubernetes",
             "git", "vscode", "代码", "软件工程", "漏洞", "安全", "编译器", "命令行"]),
]

MAX_PER_FEED = 10
MAX_ITEMS = 150
MAX_AGE_HOURS = 72


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


JUNK_SUM = ("点击查看原文", "阅读全文", "查看全文", "read more", "continue reading",
            "点击阅读", "原文链接", "查看原文", "查看更多")


def short(text: str, n: int = 150) -> str:
    text = clean_text(text)
    low = text.lower().strip(" >»…")
    if len(low) < 15 or any(j in low for j in JUNK_SUM):
        return ""
    if len(text) <= n:
        return text
    cut = text[:n]
    for sep in ("。", "！", "？", ". ", "；"):
        idx = cut.rfind(sep)
        if idx > n * 0.55:
            return cut[: idx + len(sep)].strip()
    return cut.rstrip() + "…"


def hit(blob: str, word: str) -> bool:
    """英文词按左边界匹配，避免 ai 命中 said/email；中文词直接包含。"""
    if word.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(word), blob) is not None
    return word in blob


def classify(title: str, summary: str) -> str:
    head = title.lower()
    blob = (title + " " + summary).lower()
    for cat, words in RULES:
        if any(hit(head, w) for w in words):
            return cat
    for cat, words in RULES:
        if any(hit(blob, w) for w in words):
            return cat
    return "other"


def parse_time(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime.fromtimestamp(time.mktime(val), tz=timezone.utc).astimezone(BJ)
            except Exception:
                continue
    return None


def fetch_one(feed):
    out = []
    try:
        req = Request(feed["url"], headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        })
        with urlopen(req, timeout=18) as resp:
            body = resp.read()
        parsed = feedparser.parse(body)
        for entry in parsed.entries[:MAX_PER_FEED]:
            title = clean_text(entry.get("title", ""))
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            published = parse_time(entry)
            summary = short(entry.get("summary") or entry.get("description") or "")
            out.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": feed["name"],
                "color": feed["color"],
                "ts": int(published.timestamp()) if published else 0,
                "time": published.strftime("%m-%d %H:%M") if published else "",
                "cat": classify(title, summary),
            })
        print(f"  [ok]   {feed['name']:<12} {len(out)} items", flush=True)
    except Exception as exc:
        print(f"  [skip] {feed['name']:<12} {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    return out


def collect():
    items, seen = [], set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_one, f): f for f in FEEDS}
        for fut in as_completed(futures):
            for item in fut.result():
                key = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", item["title"].lower())[:60]
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)

    cutoff = int((datetime.now(BJ) - timedelta(hours=MAX_AGE_HOURS)).timestamp())
    fresh = [i for i in items if i["ts"] >= cutoff]
    if len(fresh) < 5:
        fresh = items
    fresh.sort(key=lambda x: x["ts"], reverse=True)
    return fresh[:MAX_ITEMS]


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def render(items):
    now = datetime.now(BJ)
    build_id = now.strftime("%Y%m%d%H%M%S")
    weekdays = "一二三四五六日"
    date_cn = f"{now.year}年{now.month}月{now.day}日 星期{weekdays[now.weekday()]}"
    updated = now.strftime("%H:%M")
    sources = sorted({i["source"] for i in items})
    counts = {c: sum(1 for i in items if i["cat"] == c) for c, _ in CATS}
    counts["all"] = len(items)

    tabs = [("all", f"全部 {counts['all']}")]
    for c, label in CATS:
        if counts[c]:
            tabs.append((c, f"{label} {counts[c]}"))
    tabs_html = "".join(
        f'<button class="tab{" active" if k == "all" else ""}" data-cat="{k}">{esc(v)}</button>'
        for k, v in tabs
    )

    cards = []
    for i in items:
        cards.append(f"""<article class="card" data-cat="{i['cat']}" data-ts="{i['ts']}">
  <div class="row"><span class="src" style="--c:{i['color']}">{esc(i['source'])}</span><span class="time">{esc(i['time'])}</span></div>
  <a class="title" href="{esc(i['link'])}" target="_blank" rel="noopener noreferrer">{esc(i['title'])}</a>
  {f'<p class="sum">{esc(i["summary"])}</p>' if i['summary'] else ''}
</article>""")
    cards_html = "\n".join(cards)

    feed_list = " · ".join(esc(s) for s in sources)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="build-id" content="{build_id}">
<meta name="theme-color" content="#0a0b10">
<title>科技日报 · {now.strftime('%m-%d %H:%M')} 更新</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0a0b10;--card:#13151d;--line:#22252f;--text:#e8eaf0;--dim:#8b90a0;--accent:#4cc9f0}}
html{{-webkit-text-size-adjust:100%}}
body{{background:var(--bg);color:var(--text);font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;padding-bottom:40px}}
.wrap{{max-width:720px;margin:0 auto;padding:0 16px}}
header{{padding:28px 0 16px;position:sticky;top:0;background:linear-gradient(180deg,#0a0b10 82%,rgba(10,11,16,0));z-index:20}}
.brand{{display:flex;align-items:center;gap:8px;font-size:13px;letter-spacing:.18em;color:var(--dim);text-transform:uppercase}}
.live{{display:inline-flex;align-items:center;gap:5px;color:#22c55e;letter-spacing:.08em}}
.dot{{width:6px;height:6px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 0 rgba(34,197,94,.6);animation:pulse 2s infinite}}
@keyframes pulse{{70%{{box-shadow:0 0 0 7px rgba(34,197,94,0)}}100%{{box-shadow:0 0 0 0 rgba(34,197,94,0)}}}}
h1{{font-size:25px;line-height:1.3;margin:10px 0 6px;letter-spacing:-.01em}}
.meta{{font-size:13px;color:var(--dim)}}
.tabs{{display:flex;gap:8px;overflow-x:auto;padding:14px 0 12px;scrollbar-width:none;-webkit-overflow-scrolling:touch}}
.tabs::-webkit-scrollbar{{display:none}}
.tab{{flex:0 0 auto;border:1px solid var(--line);background:transparent;color:var(--dim);font-size:13px;padding:7px 13px;border-radius:999px;cursor:pointer;font-family:inherit;transition:.18s}}
.tab.active{{background:var(--accent);border-color:var(--accent);color:#04121a;font-weight:600}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:15px 16px;margin-bottom:10px;transition:border-color .18s}}
.card:active{{border-color:#3a3f4f}}
.row{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:7px}}
.src{{font-size:11px;font-weight:600;letter-spacing:.04em;color:var(--c);background:color-mix(in srgb,var(--c) 14%,transparent);padding:2px 8px;border-radius:6px;white-space:nowrap}}
.time{{font-size:11px;color:#6b7080;white-space:nowrap}}
.title{{display:block;color:var(--text);text-decoration:none;font-size:16.5px;font-weight:600;line-height:1.45;letter-spacing:-.005em}}
.title:active{{color:var(--accent)}}
.sum{{margin-top:7px;font-size:13.5px;line-height:1.62;color:#a3a8b8;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
footer{{margin-top:26px;padding-top:18px;border-top:1px solid var(--line);font-size:12px;color:#5f6474;line-height:1.8}}
.empty{{text-align:center;color:var(--dim);padding:60px 0;font-size:14px}}
#newbar{{position:fixed;left:50%;transform:translate(-50%,-120%);top:12px;background:var(--accent);color:#04121a;font-size:13px;font-weight:600;padding:9px 18px;border-radius:999px;box-shadow:0 8px 24px rgba(76,201,240,.3);cursor:pointer;transition:transform .3s;z-index:50}}
#newbar.show{{transform:translate(-50%,0)}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="brand"><span>科技日报</span><span class="live"><i class="dot"></i>LIVE</span></div>
  <h1>{date_cn}</h1>
  <div class="meta">最后更新 {updated} · 共 {len(items)} 条 · {len(sources)} 个来源 · 每 10 分钟自动更新</div>
</header>
<nav class="tabs">{tabs_html}</nav>
<main id="list">
{cards_html}
</main>
<footer>
  数据来源：{feed_list}<br>
  本页由 GitHub Actions 每 10 分钟自动抓取生成，纯静态托管。
</footer>
</div>
<div id="newbar">有新内容，点击刷新</div>
<script>
(function(){{
  var tabs=document.querySelectorAll('.tab'),cards=document.querySelectorAll('.card');
  function apply(cat){{
    var n=0;
    cards.forEach(function(c){{
      var hit=(cat==='all'||c.dataset.cat===cat);
      c.style.display=hit?'':'none'; if(hit)n++;
    }});
    var e=document.getElementById('empty');
    if(!n){{ if(!e){{e=document.createElement('div');e.id='empty';e.className='empty';e.textContent='这个分类暂时没有内容';document.getElementById('list').appendChild(e);}} e.style.display=''; }}
    else if(e){{e.style.display='none';}}
  }}
  tabs.forEach(function(t){{t.addEventListener('click',function(){{
    tabs.forEach(function(x){{x.classList.remove('active')}});
    t.classList.add('active'); apply(t.dataset.cat);
    window.scrollTo({{top:0,behavior:'smooth'}});
  }})}});
  var bid=document.querySelector('meta[name=build-id]').content, bar=document.getElementById('newbar');
  bar.addEventListener('click',function(){{location.reload()}});
  setInterval(function(){{
    fetch('version.txt?t='+Date.now(),{{cache:'no-store'}}).then(function(r){{return r.text()}})
      .then(function(v){{ if(v.trim()&&v.trim()!==bid) bar.classList.add('show'); }}).catch(function(){{}});
  }},90000);
}})();
</script>
</body>
</html>
"""


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "public")
    os.makedirs(out_dir, exist_ok=True)

    print("fetching feeds...", flush=True)
    items = collect()
    if len(items) < 5:
        print(f"ERROR: only {len(items)} items collected, keep previous page", flush=True)
        return 1

    page = render(items)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    build_id = re.search(r'name="build-id" content="([^"]+)"', page).group(1)
    with open(os.path.join(out_dir, "version.txt"), "w", encoding="utf-8") as f:
        f.write(build_id)

    stats = {}
    for i in items:
        stats[i["source"]] = stats.get(i["source"], 0) + 1
    print(f"done: {len(items)} items from {len(stats)} sources -> {out_dir}/index.html", flush=True)
    print("  " + ", ".join(f"{k}:{v}" for k, v in sorted(stats.items(), key=lambda x: -x[1])), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
