import requests
import pandas as pd
import time
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

# -------------------------------
# Steam
# -------------------------------
def get_steam_top_sellers(max_games=500):
    """Récupère les jeux les plus vendus sur Steam"""
    games_list = []
    page = 0
    games_per_page = 50
    headers = {"User-Agent": "Mozilla/5.0"}

    while len(games_list) < max_games:
        start = page * games_per_page
        url = f"https://store.steampowered.com/search/?filter=topsellers&start={start}&count={games_per_page}"
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            break

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, "html.parser")
        search_results = soup.find_all("a", {"class": "search_result_row"})
        if not search_results:
            break

        for result in search_results:
            data_appid = result.get("data-ds-appid")
            if data_appid:
                name_tag = result.find("span", {"class": "title"})
                name = name_tag.text.strip() if name_tag else "N/A"
                games_list.append((int(data_appid), name))
                if len(games_list) >= max_games:
                    break
        page += 1
        time.sleep(1.0)

    return games_list


def get_steam_price(app_id, country="fr"):
    """Récupère le prix sur Steam"""
    url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc={country}&l=fr"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if data[str(app_id)]["success"]:
            price_info = data[str(app_id)]["data"].get("price_overview")
            if price_info:
                return price_info["final"] / 100
            else:
                return 0
    except Exception:
        return None
    return None

# -------------------------------
# GoCleCD avec Playwright (sélection 1er lien via XPath)
# -------------------------------
def parse_price_text(text):
    """Nettoie une chaîne de prix et retourne un float en euros"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\s]", "", text).strip().replace(",", ".")
    m = re.search(r"\d+(\.\d{1,2})?", cleaned)
    return float(m.group(0)) if m else None

def accept_cookies_if_present(page):
    selectors = [
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accepter')",
        "button:has-text('Accept')",
        "text=Ok",  # certains sites FR ont juste "Ok"
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=1000)
                break
        except Exception:
            pass

def wait_for_offer_rows(page, timeout_ms=20000):
    # Attacher le tableau
    page.wait_for_selector("table#offerTable", state="attached", timeout=timeout_ms)
    # Attacher au moins une ligne
    page.wait_for_selector("table#offerTable tbody tr", state="attached", timeout=timeout_ms)
    # Scroll pour déclencher lazy-render si nécessaire
    page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
    # Attendre que la cellule prix soit visible
    price_span = page.wait_for_selector("table#offerTable tbody tr td.offers-price a span", state="visible", timeout=timeout_ms)
    return price_span

def extract_first_offer(page):
    """Extrait prix + marchand sur la première ligne du tableau des offres."""
    try:
        wait_for_offer_rows(page, timeout_ms=25000)
    except Exception:
        # fallback: donner un peu de temps et retenter une fois
        page.wait_for_timeout(1000)
        try:
            wait_for_offer_rows(page, timeout_ms=25000)
        except Exception:
            return None, None

    # Prix
    price_el = page.query_selector("table#offerTable tbody tr:nth-child(1) td.offers-price a span")
    price_text = (price_el.text_content() or "").strip() if price_el else None
    price = parse_price_text(price_text)

    # Marchand (facultatif)
    merchant_el = page.query_selector("table#offerTable tbody tr:nth-child(1) td.offers-merchant a")
    merchant = (merchant_el.text_content() or "").strip() if merchant_el else "N/A"

    return price, merchant

def get_goclecd_price(game_name):
    """Ouvre la page de résultats, sélectionne le 1er lien via XPath, visite la page produit et lit le prix dans #offerTable."""
    search_url = f"https://www.goclecd.fr/produits/?search_name={game_name.replace(' ', '+')}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(search_url)
        page.wait_for_load_state("domcontentloaded")
        accept_cookies_if_present(page)
        page.wait_for_load_state("networkidle")

        # XPath: premier <a> dans la grille des résultats (évite les échappements CSS)
        first_link_locator = page.locator("//div[contains(@class,'grid-view')]//div[1]/a")
        try:
            first_link_locator.wait_for(state="attached", timeout=10000)
        except Exception:
            # petite attente supplémentaire pour lazy-load
            page.wait_for_timeout(1500)
        count = first_link_locator.count()
        if count == 0:
            browser.close()
            return None

        product_url = first_link_locator.first.get_attribute("href")
        if not product_url:
            browser.close()
            return None

        # Charger la page produit
        page.goto(product_url)
        page.wait_for_load_state("domcontentloaded")
        accept_cookies_if_present(page)
        page.wait_for_load_state("networkidle")

        # Extraire le prix + marchand dans le tableau
        price, merchant = extract_first_offer(page)

        result = {
            "price": price,
            "currency": "EUR",
            "merchant": merchant if merchant else "N/A",
            "url": product_url
        }

        browser.close()
        return result

# -------------------------------
# Comparaison
# -------------------------------
def calculate_savings(steam_price, goclecd_price):
    if steam_price is None or goclecd_price is None or steam_price == 0:
        return None, None
    savings_eur = steam_price - goclecd_price
    savings_pct = (savings_eur / steam_price) * 100
    return savings_eur, savings_pct

def compare_prices_to_excel(games, output_file="comparaison_prix.xlsx"):
    results = []
    for app_id, name in games:
        print(f"⏳ Traitement: {name}...")
        steam_price = get_steam_price(app_id)
        goclecd_data = get_goclecd_price(name)
        goclecd_price = goclecd_data["price"] if goclecd_data else None
        merchant = goclecd_data["merchant"] if goclecd_data else "N/A"
        goclecd_url = goclecd_data["url"] if goclecd_data else ""

        savings_eur, savings_pct = calculate_savings(steam_price, goclecd_price)

        results.append({
            "Jeu": name,
            "Steam ID": app_id,
            "Prix Steam (€)": steam_price if steam_price is not None else None,
            "Prix GoCleCD (€)": goclecd_price if goclecd_price is not None else None,
            "Marchand": merchant,
            "Économie (€)": round(savings_eur, 2) if isinstance(savings_eur, (int, float)) else None,
            "Économie (%)": round(savings_pct, 2) if isinstance(savings_pct, (int, float)) else None,
            "Lien GoCleCD": goclecd_url
        })

        if savings_pct is not None:
            print(f"✅ {name}: Steam {steam_price}€ | GoCleCD {goclecd_price}€ | Marchand: {merchant} | Économie: {savings_pct:.1f}%")
        else:
            print(f"✅ {name}: Données incomplètes")

        time.sleep(0.4)

    df = pd.DataFrame(results)
    df_sorted = df.sort_values(by="Économie (%)", ascending=False, na_position='last')
    df_sorted.to_excel(output_file, index=False)
    print(f"✅ Fichier Excel créé: {output_file}")

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    print("\n" + "="*70)
    print("💰 COMPARATEUR DE PRIX STEAM vs GOCLECD")
    print("="*70 + "\n")

    max_games_input = input("Nombre de jeux à récupérer depuis Steam: ").strip()
    max_games = int(max_games_input) if max_games_input.isdigit() else 10

    games_list = get_steam_top_sellers(max_games=max_games)
    if not games_list:
        print("❌ Aucun jeu récupéré")
        raise SystemExit(0)

    print(f"\n🎮 Comparaison de {len(games_list)} jeux en cours...\n")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"comparaison_prix_{timestamp}.xlsx"
    compare_prices_to_excel(games_list, output_filename)
