#!/usr/bin/env python3
"""
agentC.py
Monitoring agent for Hyperliquid perp positions with hard-coded email alerts.
"""

import logging
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from dotenv import dotenv_values
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

# Hard-coded email credentials (per explicit requirement)
EMAIL_SENDER = "cryptosscalp@gmail.com"
EMAIL_PASSWORD = "gfke olcu ulud zpnh"
EMAIL_RECEIVER = "25harshitgarg12345@gmail.com"

TARGET_COINS = {"HYPE", "BTC", "ETH", "SOL", "XRP"}
HYPERLIQUID_URL = "https://app.hyperliquid.xyz/vaults/0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"

# python-dotenv is used strictly for static config hydration (no env variables required or read)
_DEFAULT_CONFIG = """
TABLE_SELECTOR=table:has-text('Coin')
MAX_ATTEMPTS=3
RETRY_PAUSE_SECONDS=3
"""
CONFIG = dotenv_values(stream=StringIO(_DEFAULT_CONFIG))
TABLE_SELECTOR = CONFIG.get("TABLE_SELECTOR", "table:has-text('Coin')")
MAX_ATTEMPTS = int(CONFIG.get("MAX_ATTEMPTS", 3))
RETRY_PAUSE_SECONDS = int(CONFIG.get("RETRY_PAUSE_SECONDS", 3))

HEADER_ALIASES = {
    "coin": ("coin", "asset", "market", "pair"),
    "leverage": ("leverage", "lev"),
    "size": ("size", "position size", "qty", "quantity"),
    "mark_price": ("mark price", "mark", "price"),
    "pnl_roe": ("pnl (roe %)", "pnl", "roe"),
    "position_value": ("position value", "value", "notional"),
}
REQUIRED_COLUMNS = {"coin", "leverage", "size", "mark_price", "pnl_roe"}
SCALE_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
NUMERIC_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")

class AgentCError(Exception):
    """Custom exception for agentC-specific failures."""

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("playwright").setLevel(logging.WARNING)

def collect_positions_via_playwright() -> List[Dict[str, object]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        context = browser.new_context()
        page = context.new_page()
        try:
            return fetch_positions_from_page(page)
        finally:
            context.close()
            browser.close()

def fetch_positions_from_page(page) -> List[Dict[str, object]]:
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            logging.info("Loading Hyperliquid vault page (attempt %s/%s)...", attempt, MAX_ATTEMPTS)
            page.goto(HYPERLIQUID_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                logging.debug("Network idle wait timed out; continuing.")
            page.wait_for_selector(TABLE_SELECTOR, state="visible", timeout=25000)
            table_locators = page.locator(TABLE_SELECTOR)
            if table_locators.count() == 0:
                raise AgentCError("Positions table selector returned no matches.")
            table_html = table_locators.first.evaluate("element => element.outerHTML")
            positions = parse_positions_from_html(table_html)
            logging.info("Parsed %d perp positions from the table.", len(positions))
            return positions
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            last_error = exc
            logging.warning("Playwright issue on attempt %s: %s", attempt, exc)
        except AgentCError as exc:
            last_error = exc
            logging.warning("Parsing issue on attempt %s: %s", attempt, exc)
        if attempt < MAX_ATTEMPTS:
            logging.info("Retrying in %s seconds...", RETRY_PAUSE_SECONDS)
            time.sleep(RETRY_PAUSE_SECONDS)
    raise AgentCError(f"Unable to collect positions after {MAX_ATTEMPTS} attempts: {last_error}")

def parse_positions_from_html(html: str) -> List[Dict[str, object]]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table") or [soup]
    positions: List[Dict[str, object]] = []

    for table in tables:
        header_cells = table.find_all("th")
        if not header_cells:
            continue
        header_texts = [cell.get_text(separator=" ", strip=True) for cell in header_cells]
        header_map = map_headers(header_texts)
        if not REQUIRED_COLUMNS.issubset(header_map.keys()):
            continue

        tbody = table.find("tbody")
        if tbody:
            data_rows = tbody.find_all("tr")
        else:
            all_rows = table.find_all("tr")
            data_rows = all_rows[1:] if len(all_rows) > 1 else []

        for row in data_rows:
            cells = row.find_all("td")
            if not cells:
                cells = row.select('[role="cell"], [role="gridcell"]')
            if not cells:
                continue

            coin_raw = get_cell_text(cells, header_map.get("coin"))
            if not coin_raw:
                continue
            coin_symbol = clean_coin_symbol(coin_raw)
            if not coin_symbol:
                continue

            leverage_text = get_cell_text(cells, header_map.get("leverage")) or "N/A"
            size_text = get_cell_text(cells, header_map.get("size")) or "N/A"
            mark_price_text = get_cell_text(cells, header_map.get("mark_price")) or "N/A"
            pnl_text = get_cell_text(cells, header_map.get("pnl_roe")) or "N/A"

            size_num = parse_numeric_value(size_text)
            mark_price_num = parse_numeric_value(mark_price_text)

            position_value_text = "Unavailable"
            position_value_num: Optional[float] = None
            value_source = "unavailable"

            if "position_value" in header_map:
                scraped_value = get_cell_text(cells, header_map.get("position_value"))
                if scraped_value:
                    position_value_text = scraped_value
                    position_value_num = parse_numeric_value(scraped_value)
                    value_source = "scraped"

            if position_value_num is None and size_num is not None and mark_price_num is not None:
                position_value_num = size_num * mark_price_num
                position_value_text = format_notional(position_value_num)
                value_source = "computed"
            elif position_value_num is not None and position_value_text == "Unavailable":
                position_value_text = format_notional(position_value_num)

            positions.append(
                {
                    "coin_display": coin_raw.strip(),
                    "coin_symbol": coin_symbol,
                    "leverage": leverage_text,
                    "size_text": size_text,
                    "size_num": size_num,
                    "mark_price_text": mark_price_text,
                    "mark_price_num": mark_price_num,
                    "pnl_roe": pnl_text,
                    "position_value_text": position_value_text,
                    "position_value_num": position_value_num,
                    "position_value_source": value_source,
                }
            )
    if not positions:
        logging.warning("No rows were parsed from the positions table.")
    return positions

def map_headers(header_texts: List[str]) -> Dict[str, int]:
    header_map: Dict[str, int] = {}
    for idx, header in enumerate(header_texts):
        normalized = normalize_header_text(header)
        for key, aliases in HEADER_ALIASES.items():
            if key in header_map:
                continue
            if any(alias in normalized for alias in aliases):
                header_map[key] = idx
                break
    return header_map

def normalize_header_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())

def get_cell_text(cells, index: Optional[int]) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index].get_text(separator=" ", strip=True)

def clean_coin_symbol(label: str) -> str:
    text = label.upper().strip()
    text = text.replace("PERP", "")
    for splitter in ("/", "-", " "):
        if splitter in text:
            text = text.split(splitter)[0]
    text = re.sub(r"[^A-Z0-9]", "", text)
    return text

def parse_numeric_value(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = text.replace(",", "").replace("−", "-").strip().upper()
    if not cleaned:
        return None
    match = NUMERIC_PATTERN.search(cleaned)
    if not match:
        return None
    value = float(match.group())
    suffix_index = match.end()
    suffix = cleaned[suffix_index:].strip()
    if suffix:
        first = suffix[0]
        if first in SCALE_SUFFIXES:
            value *= SCALE_SUFFIXES[first]
    return value

def format_notional(value: Optional[float]) -> str:
    if value is None:
        return "Unavailable"
    abs_value = abs(value)
    decimals = 2 if abs_value >= 1 else 6
    return f"${value:,.{decimals}f}"

def dispatch_email(target_positions: List[Dict[str, object]], all_positions: List[Dict[str, object]]) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if target_positions:
        subject = "agentC Alert: Target Hyperliquid perp positions detected"
        body = build_positive_body(target_positions, all_positions, timestamp)
    else:
        subject = "agentC Update: Target Hyperliquid perp positions absent"
        body = build_negative_body(all_positions, timestamp)
    send_email(subject, body)

def build_positive_body(
    target_positions: List[Dict[str, object]],
    all_positions: List[Dict[str, object]],
    timestamp: str,
) -> str:
    lines = [
        f"agentC scan completed at {timestamp} UTC.",
        "",
        "The following target coins are currently in the vault's perp positions:",
    ]
    for position in target_positions:
        lines.extend(format_position_lines(position))
        lines.append("")
    lines.append(f"Total perp positions inspected: {len(all_positions)}")
    return "\n".join(line for line in lines if line is not None).strip()

def build_negative_body(all_positions: List[Dict[str, object]], timestamp: str) -> str:
    lines = [
        "None of your target coins (HYPE, BTC, ETH, SOL, XRP) are present in the account's perp positions.",
        "",
        f"Scan completed at {timestamp} UTC.",
        f"Total perp positions inspected: {len(all_positions)}",
    ]
    if all_positions:
        lines.append("")
        lines.append("Visible perp positions:")
        for position in all_positions:
            lines.extend(format_position_lines(position))
            lines.append("")
    return "\n".join(line for line in lines if line is not None).strip()

def format_position_lines(position: Dict[str, object]) -> List[str]:
    value_descriptor = position.get("position_value_text", "Unavailable")
    if position.get("position_value_source") == "computed":
        value_descriptor = f"{value_descriptor} (computed)"
    return [
        f"- Coin: {position.get('coin_display')} (symbol: {position.get('coin_symbol')})",
        f"  Leverage: {position.get('leverage')}",
        f"  Size: {position.get('size_text')}",
        f"  Mark Price: {position.get('mark_price_text')}",
        f"  PNL (ROE %): {position.get('pnl_roe')}",
        f"  Position Value: {value_descriptor}",
    ]

def send_email(subject: str, body: str) -> None:
    logging.info("Sending email alert: %s", subject)
    message = f"Subject: {subject}\nFrom: {EMAIL_SENDER}\nTo: {EMAIL_RECEIVER}\n\n{body}"
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], message.encode("utf-8"))
    logging.info("Email delivered successfully.")

def main() -> int:
    configure_logging()
    logging.info("agentC monitoring run started.")
    try:
        positions = collect_positions_via_playwright()
    except Exception as exc:
        logging.exception("Failed to collect positions: %s", exc)
        return 1

    target_positions = [pos for pos in positions if pos.get("coin_symbol") in TARGET_COINS]
    logging.info("Scraped %d positions; %d matched target coins.", len(positions), len(target_positions))

    try:
        dispatch_email(target_positions, positions)
    except Exception as exc:
        logging.exception("Failed to send alert email: %s", exc)
        return 1

    logging.info("agentC monitoring run completed successfully.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
