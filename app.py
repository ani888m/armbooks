import os
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
import requests
import uuid
from models import User


from flask import (
    Flask, render_template, request,
    redirect, flash, abort,
    session, url_for, jsonify
)

from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Modelle importieren
from models import db, Bestellung, BestellPosition, NewsletterSubscriber,  Gutschein, Workshop, WorkshopSlot, WorkshopBuchung
from models import Produkt


from datetime import timedelta

from functools import lru_cache

import re

import secrets
import string


from sitemap_generator import build_sitemap_xml
import xml.etree.ElementTree as ET
from flask import Response


from gutschein_pdf import send_gutschein_email

from werkzeug.security import generate_password_hash, check_password_hash


# =====================================================
# CONFIG
# =====================================================

load_dotenv()

app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app)

PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")

PAYPAL_BASE = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_MODE == "sandbox"
    else "https://api-m.paypal.com"
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=1)
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") == "production"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_PERMANENT"] = False

app.config["PAYPAL_CLIENT_ID"] = PAYPAL_CLIENT_ID


app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY")

if not app.config["SECRET_KEY"]:
    raise RuntimeError("FLASK_SECRET_KEY fehlt!")

database_url = os.getenv("DATABASE_URL", "sqlite:///ibk-shop-db.db")
database_url = database_url.replace("postgres://", "postgresql://")

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
with app.app_context():
    db.create_all()

# ← NEU: Bilder beim Start konvertieren
from bildcomprim import walk_and_optimize
walk_and_optimize("static/images")



ADMIN_PASSWORD = os.getenv("FLASK_ADMIN_PASSWORD")

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

csrf = CSRFProtect(app)



# ---------- BUCHBUTLER API ZUGANG ----------



BUCHBUTLER_USER = os.getenv("BUCHBUTLER_USER")
BUCHBUTLER_PASSWORD = os.getenv("BUCHBUTLER_PASSWORD")

BUCHBUTLER_MOL_KUNDE_ID = os.getenv("BUCHBUTLER_MOL_KUNDE_ID")
BUCHBUTLER_RECHNUNGSADRESSE_ID = os.getenv("BUCHBUTLER_RECHNUNGSADRESSE_ID", "1")
BUCHBUTLER_VERKAUFSKANAL_ID = os.getenv("BUCHBUTLER_VERKAUFSKANAL_ID", "1")

BASE_URL = "https://api.buchbutler.de"




@app.route("/login", methods=["GET", "POST"])
@csrf.exempt
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            flash("Login fehlgeschlagen", "error")
            return redirect("/login")

        session["user_id"] = user.id
        return redirect("/")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect("/")
# =====================================================
# Gutschein
# =====================================================

@app.route("/admin/gutscheine")
def admin_gutscheine():
    if not session.get("admin"):
        abort(403)
    alle = Gutschein.query.all()
    return "<br>".join(
        f"{g.code} | Wert: {g.wert}€ | Rest: {g.restwert}€ | Aktiv: {g.aktiv}"
        for g in alle
    ) or "Keine Gutscheine in der Datenbank"




# GUTSCHEIN CODE GENERATOR
# =====================================================

def generate_gutschein_code(length=12):
    alphabet = string.ascii_uppercase + string.digits

    while True:
        code = ''.join(secrets.choice(alphabet) for _ in range(length))

        exists = Gutschein.query.filter_by(code=code).first()
        if not exists:
            return code


# GUTSCHEIN PRODUKT (IM WARENKORB KAUFEN)
# =====================================================



@app.route("/gutschein", methods=["GET", "POST"])
@csrf.exempt
def gutschein():

    if request.method == "GET":
        return render_template("gutschein.html")

    try:
        betrag = float(request.form.get("betrag", 0))

    except ValueError:
        flash("Ungültiger Betrag", "error")
        return redirect("/gutschein")

    email = request.form.get("email")

    if not email:
        flash("Bitte Email eingeben", "error")
        return redirect("/gutschein")

    # WICHTIG
    session["checkout_email"] = email
    session.pop("gutschein", None)  # ← NEU: alten Gutschein löschen


    # Gutschein als Warenkorbprodukt
    session["cart"] = [{
        "id": 999999,
        "title": "Geschenkgutschein",
        "price": betrag,
        "quantity": 1,
        "is_gutschein": True
    }]

    session.modified = True

    return jsonify({"success": True})

# GUTSCHEIN ANWENDEN (CHECKOUT / CART)
# =====================================================

@app.route("/apply-gutschein", methods=["POST"])
@csrf.exempt
def apply_gutschein():
    body = request.json
    code = (body.get("code") or "").strip().upper()
    
    gutschein = Gutschein.query.filter_by(code=code).first()

    if not gutschein:
        return jsonify({"success": False, "message": "Ungültiger Code"})

    if not gutschein.ist_gueltig():
        return jsonify({"success": False, "message": "Gutschein ungültig oder abgelaufen"})

    # ← Cart aus Request nehmen, nicht aus Session!
    cart = body.get("cart") or get_cart()

    total = sum(
        item["price"] * item.get("quantity", 1)
        for item in cart
    )

    rabatt = min(total, gutschein.restwert)

    # Cart auch gleich in Session speichern (sync)
    if body.get("cart"):
        session["cart"] = body["cart"]
        session.modified = True

    session["gutschein"] = {
        "code": gutschein.code,
        "betrag": rabatt
    }
    session.modified = True

    return jsonify({
        "success": True,
        "rabatt": rabatt,
        "new_total": max(total - rabatt, 0)
    })


# TOTAL BERECHNUNG (OHNE GUTSCHEIN!)
# =====================================================

def calculate_total(cart):

    total = sum(
        item["price"] * item["quantity"]
        for item in cart
    )

    gutschein = session.get("gutschein")

    gutschein_wert = 0

    if gutschein:
        gutschein_wert = float(
            gutschein.get("betrag", 0)
        )

    return max(total - gutschein_wert, 0)


# OPTIONAL: GUTSCHEIN AUS SESSION HOLEN
# =====================================================

def get_gutschein():
    return session.get("gutschein")


# =====================================================
# PRODUKTE LADEN
# =====================================================

basedir = os.path.abspath(os.path.dirname(__file__))
json_path = os.path.join(basedir, "produkte.json")

if os.path.exists(json_path):
    with open(json_path, encoding="utf-8") as f:
        produkte = json.load(f)
else:
    produkte = []







# =====================================================
# PAYPAL
# =====================================================

def paypal_access_token():
    response = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={"grant_type": "client_credentials"},
    )
    return response.json().get("access_token")





@app.route("/create-paypal-order", methods=["POST"])
@csrf.exempt
def create_paypal_order():
    cart_items = get_cart()
    total = calculate_total(cart_items)

    if not cart_items:
        return jsonify({"error": "Warenkorb leer"}), 400

    # WICHTIG:
    # Wenn Gutschein alles bezahlt hat
    # dann KEIN PayPal starten
    if total <= 0:

        return jsonify({
            "free_checkout": True
        })

    access_token = paypal_access_token()

    response = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": "EUR",
                    "value": f"{total:.2f}"
                }
            }]
        },
    )

    order_data = response.json()

    if "id" not in order_data:
        logger.error(f"PayPal Fehler: {order_data}")
        return jsonify({
            "error": "PayPal order creation failed",
            "details": order_data
        }), 400

    return jsonify({"id": order_data["id"]})




@app.route("/capture-paypal-order/<order_id>", methods=["POST"])
@csrf.exempt
def capture_paypal_order(order_id):

    try:

        # -----------------------------------
        # PayPal Capture
        # -----------------------------------

        access_token = paypal_access_token()

        response = requests.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
        )

        data = response.json()

        if data.get("status") != "COMPLETED":
            return jsonify({
                "status": "error",
                "message": "PayPal-Zahlung nicht abgeschlossen",
                "data": data
            }), 400

        # -----------------------------------
        # Warenkorb laden
        # -----------------------------------

        cart_items = get_cart()

        if not cart_items:
            return jsonify({
                "status": "error",
                "message": "Warenkorb leer"
            }), 400

        # -----------------------------------
        # Bestellung speichern
        # -----------------------------------

        bestellung = Bestellung(
            email=session.get("checkout_email"),
            vorname=session.get("checkout_vorname"),
            nachname=session.get("checkout_nachname"),
            strasse=session.get("checkout_strasse"),
            hausnummer=session.get("checkout_hausnummer"),
            plz=session.get("checkout_plz"),
            stadt=session.get("checkout_stadt"),
            land=session.get("checkout_land"),
            telefon=session.get("checkout_telefon"),
            paymentmethod="paypal"
        )

        db.session.add(bestellung)

        # Damit bestellung.id sofort existiert
        db.session.flush()

        # -----------------------------------
        # Gutschein aus Session anwenden
        # -----------------------------------

        gutschein_session = session.get("gutschein")

        if gutschein_session:

            gutschein = Gutschein.query.filter_by(
                code=gutschein_session["code"]
            ).first()

            if gutschein and gutschein.ist_gueltig():

                verwendeter_betrag = float(
                    gutschein_session["betrag"]
                )

                gutschein.restwert -= verwendeter_betrag

                if gutschein.restwert <= 0:
                    gutschein.restwert = 0
                    gutschein.aktiv = False

        # -----------------------------------
        # Bestellpositionen speichern
        # -----------------------------------

        for item in cart_items:

            db.session.add(
                BestellPosition(
                    bestellung_id=bestellung.id,
                    bezeichnung=item["title"],
                    menge=item["quantity"],
                    preis=item["price"]
                )
            )

            # -----------------------------------
            # Gutschein erzeugen
            # -----------------------------------

            if item.get("is_gutschein"):

                code = generate_gutschein_code()

                gutschein = Gutschein(
                    code=code,
                    wert=item["price"],
                    restwert=item["price"],
                    aktiv=True,
                    empfaenger_email=session.get("checkout_email"),
                    bestellung_id=bestellung.id
                )

                db.session.add(gutschein)

                try:


                    send_gutschein_email(
                        recipient=session.get("checkout_email"),
                        code=code,
                        betrag=item["price"],
                    )

                   

                except Exception:
                    logger.exception(
                        "Gutschein Email fehlgeschlagen"
                    )

        # -----------------------------------
        # Alles speichern
        # -----------------------------------

        db.session.commit()

       # -----------------------------------
        # Nur echte Bücher an BuchButler
        # senden (kein Gutschein, kein Offline-Produkt)
        # -----------------------------------

        buch_items = [
            item for item in cart_items
            if not item.get("is_gutschein") and item.get("ean")
        ]

        if buch_items:

            try:
                sende_bestellung_an_buchbutler(
                    bestellung,
                    buch_items
                )

            except Exception:
                logger.exception(
                    "BuchButler Bestellung fehlgeschlagen"
                )
        # -----------------------------------
        # Session aufräumen
        # -----------------------------------

        session.pop("cart", None)
        session.pop("gutschein", None)

        # -----------------------------------
        # Erfolgreich
        # -----------------------------------

        return jsonify({
            "status": "success",
            "order_id": order_id
        })

    except Exception as e:

        logger.exception(
            "Fehler beim Capturen der PayPal-Zahlung"
        )

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500



def verify_webhook(headers, body):

    access_token = paypal_access_token()

    response = requests.post(
        f"{PAYPAL_BASE}/v1/notifications/verify-webhook-signature",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
            "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
            "cert_url": headers.get("PAYPAL-CERT-URL"),
            "auth_algo": headers.get("PAYPAL-AUTH-ALGO"),
            "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
            "webhook_id": PAYPAL_WEBHOOK_ID,
            "webhook_event": body
        }
    )

    return response.json().get("verification_status") == "SUCCESS"



@app.route("/paypal-webhook", methods=["POST"])
@csrf.exempt
def paypal_webhook():

    body = request.get_data(as_text=True)
    event = json.loads(body)
    headers = request.headers

    if not verify_webhook(headers, body):
        return "", 400

    event_type = event.get("event_type")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        capture = event["resource"]
        order_id = capture["supplementary_data"]["related_ids"]["order_id"]
        amount = capture["amount"]["value"]
        logger.info(f"PayPal Zahlung abgeschlossen: {order_id} – {amount} EUR")



    return "", 200



# -----------------------------
# CONTENT API
# -----------------------------
@lru_cache(maxsize=128)
def cached_lade_produkt_von_api(ean):
    return lade_produkt_von_api(ean)


def lade_produkt_von_api(ean):

    if not check_auth():
        return None

    try:
        res = buchbutler_request("CONTENT", ean)

        if not res:
            return None

        attrs = res.get("Artikelattribute") or {}
      

        # 🔥 Bild (nur BuchButler Image API)
        bild_url = f"https://api.buchbutler.de/image/{ean}"

        produkt = {
            "id": to_int(res.get("pim_artikel_id")),
            "ean": ean,
            "name": res.get("bezeichnung"),
            "autor": attr(attrs, "Autor"),
            "illustrator": attr(attrs, "Illustrator"),
            "preis": to_float(res.get("vk_brutto")),
            "isbn": attr(attrs, "ISBN_13"),
            "seiten": attr(attrs, "Seiten"),
            "format": attr(attrs, "Buchtyp"),
            "sprache": attr(attrs, "Sprache"),
            "verlag": attr(attrs, "Verlag"),
            "erscheinungsjahr": attr(attrs, "Erscheinungsjahr"),
            "erscheinungsdatum": attr(attrs, "Erscheinungsdatum"),
            "beschreibung": res.get("text_text"),
            "alter_von": attr(attrs, "Altersempfehlung_von"),
            "alter_bis": attr(attrs, "Altersempfehlung_bis"),          
            # 🔥 Maße
            "laenge": attr(attrs, "Laenge"),
            "breite": attr(attrs, "Breite"),
            "hoehe": attr(attrs, "Hoehe"),

            # 🔥 Gewicht


            "gewicht": attr(attrs, "Gewicht"),


            # 🔥 Bilder
            "bilder": [bild_url],

            "extra": attrs
        }

        return produkt

    except Exception:
        logger.exception("Fehler beim Laden von CONTENT API")
        return None

# -----------------------------
# MOVEMENT API
# -----------------------------

def lade_bestand_von_api(ean):
    """Lädt Bestand / Preis / Lieferdaten"""

    if not check_auth():
        return None

    try:
        res = buchbutler_request("MOVEMENT", ean)

        if not res:
            return None

        # 🔥 FIX — falls Liste zurückkommt
        if isinstance(res, list):
            if len(res) == 0:
                return None
            res = res[0]

     
        return {
            "bestand": to_int(res.get("Bestand")),
            "preis": to_float(res.get("Preis")),
            "erfuellungsrate": res.get("Erfuellungsrate"),
            "handling_zeit": res.get("Handling_Zeit_in_Werktagen"),
            "ausverkauft": ist_ausverkauft(res.get("Handling_Zeit_in_Werktagen"))

        }


    except Exception:
        logger.exception("Fehler beim Laden von MOVEMENT API")
        return None

# -----------------------------
# Bestellung an Buchbutler senden 
# -----------------------------

def sende_bestellung_an_buchbutler(bestellung, cart_items):

    url = f"{BASE_URL}/ORDER/"

    collectkey = str(uuid.uuid4())
    bestellung.collectkey = collectkey
    db.session.commit()
    # PayPal = bereits bezahlt → Vorkasse (1)
    # Rechnung = Buchbutler rechnet ab → Rechnung (2)
    mol_zahlart_id = 2 

    
    payload = {
        "username": BUCHBUTLER_USER,
        "passwort": BUCHBUTLER_PASSWORD,

        "auftrag_kopf": {
            "mol_kunde_id": int(BUCHBUTLER_MOL_KUNDE_ID),
            "rechnungsadresse_id": int(BUCHBUTLER_RECHNUNGSADRESSE_ID),
            "mol_zahlart_id": mol_zahlart_id,  # ← dynamisch
            "bestelldatum": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bestellreferenz": f"IBK-{bestellung.id}",
            "seite": "ibk-bilderbuch.de",
            "bestellfreigabe": 1,
            "mol_verkaufskanal_id": int(BUCHBUTLER_VERKAUFSKANAL_ID)
        },

        "lieferadresse": {
            "anrede": "",
            "vorname": bestellung.vorname,
            "nachname": bestellung.nachname,
            "strasse": bestellung.strasse,
            "hausnummer": bestellung.hausnummer,
            "plz": bestellung.plz,
            "ort": bestellung.stadt,
            "land": bestellung.land,
            "land_iso": "DE",
            "tel": bestellung.telefon
        },

        "auftrag_position": [],

        "auftrag_zusatz": [
            {
                "typ": "SHIPPING_OPTION",
                "value": "1040"
            },
            {
                "typ": "collectkey",
                "value": collectkey
            }
        ]
    }

    

    for i, item in enumerate(cart_items):
        payload["auftrag_position"].append({
            "ean": item["ean"],
            "pos_bezeichnung": item["title"],
            "menge": item["quantity"],
            "ek_netto": 0,
            "vk_brutto": item["price"],
            "pos_referenz": f"{bestellung.id}-{i}"
        })
    
    response = requests.post(url, json=payload, timeout=20)
    
    data = response.json()
    
    if data.get("import_hash"):
        bestellung.moluna_order_id = data["import_hash"]
        bestellung.moluna_status = "übermittelt"
        db.session.commit()
        
    logger.info("Buchbutler Bestellung: %s", data)
    return data
    
    
def buchbutler_orderresponse(collectkey):

    url = f"{BASE_URL}/ORDERRESPONSE/"

    payload = {
        "username": BUCHBUTLER_USER,
        "passwort": BUCHBUTLER_PASSWORD,
        "collectkey": collectkey
    }

    try:
        response = requests.post(url, json=payload, timeout=10)

        # Wenn keine erfolgreiche Antwort
        if response.status_code != 200:
            logger.warning(f"ORDERRESPONSE Statuscode: {response.status_code}")
            return None

        # Wenn Antwort leer ist
        if not response.text.strip():
            logger.info("ORDERRESPONSE leer")
            return None

        return response.json()

    except Exception:
        logger.exception("ORDERRESPONSE Fehler")
        return None
# =====================================================
# ROUTES
# =====================================================

# sitemap.xml

@app.route("/sitemap.xml")
def sitemap():
    xml_element = build_sitemap_xml()

    xml_str = ET.tostring(xml_element, encoding="utf-8", method="xml")

    return Response(xml_str, mimetype="application/xml")

# Admin Test
@app.route("/admin-test")
def admin_test():
    alle = Bestellung.query.all()
    return {"anzahl_bestellungen": len(alle)}



def admin_required():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    return None



@limiter.limit("5 per minute")
@app.route("/ibk-control-8471", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password")
        if pw == ADMIN_PASSWORD:
            session.clear()
            session["admin"] = True
            session.permanent = True
            return redirect("/admin/bestellungen")
        else:
            flash("Falsches Passwort!", "error")
    return render_template("admin_login.html")



# Admin Bestellungen anzeigen




@app.route("/admin/bestellungen")
def admin_bestellungen():

    resp = admin_required()
    if resp:
        return resp

    alle = Bestellung.query.order_by(Bestellung.bestelldatum.desc()).all()

    for b in alle:

        if getattr(b, "collectkey", None):

            response = buchbutler_orderresponse(b.collectkey)

            if response and "response" in response:
                status = response["response"].get("status")
                lieferungen = response["response"].get("lieferungen", [])

                # Status speichern
                b.moluna_status = status if status else "unbekannt"

                # Trackingnummern & andere Felder sammeln
                trackingnummern = []
                logistiker_list = []
                paketart_list = []
                eans = []

                for lieferung in lieferungen:
                    if lieferung.get("trackingnummer"):
                        trackingnummern.append(lieferung["trackingnummer"])
                    if lieferung.get("logistiker"):
                        logistiker_list.append(lieferung["logistiker"])
                    if lieferung.get("logistik_produkt"):
                        paketart_list.append(lieferung["logistik_produkt"])
                    if lieferung.get("ean"):
                        eans.append(lieferung["ean"])

                # Optional: als kommagetrennte Strings speichern
                b.trackingnummer = ", ".join(trackingnummern) if trackingnummern else None
                b.logistiker = ", ".join(logistiker_list) if logistiker_list else None
                b.paketart = ", ".join(paketart_list) if paketart_list else None
                b.eans = ", ".join(eans) if eans else None

            else:
                b.moluna_status = "keine Antwort"
                b.trackingnummer = None
                b.logistiker = None
                b.paketart = None
                b.eans = None

    # Commit nach allen Updates
    db.session.commit()

    return render_template(
        "admin_bestellungen.html",
        bestellungen=alle
    )


@app.route("/admin/sync-buchbutler/<int:index>")
def sync_buchbutler(index):

    if not session.get("admin"):
        abort(403)

    if index >= len(produkte):
        return "✅ Sync komplett"

    produkt = produkte[index]
    ean = produkt.get("ean")

    if ean:
        print("SYNC:", ean)

        api = lade_produkt_von_api(ean)
        movement = lade_bestand_von_api(ean)

        if api:
            produkt["name"] = api.get("name")
            produkt["autor"] = api.get("autor")

       

        if movement:
            produkt["preis"] = movement.get("preis")
            produkt["handling_zeit"] = movement.get("handling_zeit")
            produkt["ausverkauft"] = movement.get("ausverkauft", False)

        # sofort speichern → kein RAM Wachstum
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(produkte, f, ensure_ascii=False, indent=2)

    next_index = index + 1

    return redirect(url_for("sync_buchbutler", index=next_index))
    
# suche icon 
@app.route("/suche", methods=["GET", "POST"])
def suche():
    query = ""
    ergebnisse = []

    if request.method == "POST":
        query = request.form.get("q", "").lower()

        for produkt in produkte:
            name = produkt.get("name", "").lower()

            if query in name:
                ergebnisse.append(produkt)

    return render_template(
        "suche.html",
        query=query,
        ergebnisse=ergebnisse
    )


@app.route("/<kategorietype>/<name>")
def kategorie(kategorietype, name):
    name = name.replace('-', ' ')
    ergebnisse = []
    for produkt in produkte:
        kategorien = produkt.get("kategorie", [])
        kategorien_lower = [k.lower() for k in kategorien]
        if name.lower() in kategorien_lower:
            ergebnisse.append(produkt)
    return render_template("kategorie.html", produkte=ergebnisse, titel=name)



# Produkt Detail

def slugify(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9äöüßé ]', '', text)
    return text.replace(" ", "-")

for p in produkte:
    if not p.get("slug"):
        p["slug"] = slugify(p.get("name", "produkt"))

with open("produkte.json", "w", encoding="utf-8") as f:
    json.dump(produkte, f, ensure_ascii=False, indent=2)

app.template_filter('slugify')(slugify)

@app.route('/produkt/<int:produkt_id>/<slug>')
def produkt_detail(produkt_id, slug):

    lokale_daten = next(
        (p.copy() for p in produkte if p["id"] == produkt_id),
        None
    )

    if not lokale_daten:
        abort(404)

    # ✅ richtigen slug berechnen
    richtiger_slug = lokale_daten.get("slug")

    # 🔥 WICHTIG: redirect wenn falsch
    if slug != richtiger_slug:
        return redirect(url_for(
            "produkt_detail",
            produkt_id=produkt_id,
            slug=richtiger_slug
        ), code=301)

    ean = lokale_daten.get("ean")


       # ── NEU: Offline-Produkt (kein EAN) ──────────────────────────
    if not ean:
        produkt = lokale_daten.copy()
        produkt.setdefault("bestand", "n/a")
        produkt.setdefault("preis", lokale_daten.get("preis", 0))
        produkt.setdefault("handling_zeit", "n/a")
        produkt.setdefault("erfuellungsrate", "n/a")
        return render_template("produkt.html", produkt=produkt)
    # ─────────────────────────────────────────────────────────────

   

    produkt = cached_lade_produkt_von_api(ean)

    if not produkt:
        abort(404)

    movement = lade_bestand_von_api(ean)
    if movement:
        produkt.update(movement)

    produkt.update(lokale_daten)


    produkt.setdefault("bestand", "n/a")
    produkt.setdefault("preis", 0)
    produkt.setdefault("handling_zeit", "n/a")
    produkt.setdefault("erfuellungsrate", "n/a")
    produkt.setdefault("ausverkauft", False)

    return render_template(
        "produkt.html",
        produkt=produkt
    )

# ============================
# CART ROUTES
# ============================

@app.route("/add-to-cart", methods=["POST"])
def add_to_cart():
    produkt_id = int(request.form.get("produkt_id"))
    produkt = next((p for p in produkte if p["id"] == produkt_id), None)

    if not produkt:
        abort(404)

    # ✅ WICHTIG: Kopie machen (kein globales Update!)
    produkt = produkt.copy()


    
    # ── NEU: nur API-Aufruf wenn EAN vorhanden ───────────────────
    if produkt.get("ean"):
        movement = lade_bestand_von_api(produkt["ean"])
        if movement:
            produkt.update(movement)
    # preis bleibt sonst aus JSON (lokale_daten["preis"])
    # ─────────────────────────────────────────────────────────────


    if produkt.get("ausverkauft"):
        flash("Dieses Produkt ist leider ausverkauft.", "error")
        return redirect(request.referrer or url_for("index"))


    cart = get_cart()

    found = False
    for item in cart:
        if item["id"] == produkt_id:
            item["quantity"] += 1
            found = True
            break

  

    if not found:
        cart.append({
            "id": produkt["id"],
            "title": produkt["name"],
            "price": produkt.get("preis", 0),
            "quantity": 1,
            "ean": produkt.get("ean", "")  # ← sicher
        })
        

    save_cart(cart)
    return redirect(url_for("cart"))



@app.route("/cart")
def cart():
    cart_items = get_cart()
    total = calculate_total(cart_items)
    return render_template("cart.html", cart_items=cart_items, total=total)

@app.route("/remove-from-cart/<int:produkt_id>")
def remove_from_cart(produkt_id):
    cart = get_cart()
    cart = [item for item in cart if item["id"] != produkt_id]
    save_cart(cart)
    return redirect(url_for("cart"))


@app.route("/sync-cart", methods=["POST"])
@csrf.exempt  
def sync_cart():
    data = request.get_json()

    if not data:
        return {"status": "error"}, 400

    session["cart"] = data
    session.modified = True

    print("SYNCED CART:", session["cart"])

    return {"status": "ok"}
    
# ============================
# CHECKOUT
# ============================

@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    cart_items = get_cart()
    total = calculate_total(cart_items)

    logger.info("Checkout gestartet")

    if request.method == "POST":

        email = request.form.get("email")

        if not email or not cart_items:
            flash("Bitte gültige Daten eingeben.", "error")
            return redirect(url_for("checkout"))


         # Kunde erfassen / Punkte vergeben
        if "user_id" in session:
            user = User.query.get(session["user_id"])
            punkte = int(total)  # Beispiel: 1€ = 1 Punkt
            user.punkte += punkte

            # Gutschein vergeben bei 100 Punkten
            if user.punkte >= 100:
                code = str(uuid.uuid4())[:8]
                gutschein = Gutschein(code=code, wert=10, user_id=user.id)
                user.punkte -= 100
                db.session.add(gutschein)
                send_email(
                    subject="Dein Gutschein 🎁",
                    body=f"Dein Code: {code}",
                    recipient=user.email
                )
            db.session.commit()

        try:
            session["checkout_email"] = request.form.get("email")
            session["checkout_vorname"] = request.form.get("vorname")
            session["checkout_nachname"] = request.form.get("nachname")
            session["checkout_strasse"] = request.form.get("strasse")
            session["checkout_hausnummer"] = request.form.get("hausnummer")
            session["checkout_plz"] = request.form.get("plz")
            session["checkout_stadt"] = request.form.get("stadt")
            session["checkout_land"] = request.form.get("land")
            session["checkout_telefon"] = request.form.get("telefon")
            session["checkout_adresszusatz"] = request.form.get("adresszusatz")

            logger.info("Kundendaten für PayPal gespeichert")

        except Exception as e:
            logger.error(f"Checkout Fehler: {e}")
            flash("Fehler beim Checkout.", "error")
            return redirect(url_for("checkout"))

    return render_template(
        "checkout.html",
        cart_items=cart_items,
        total=total
    )


@app.route("/checkouttest", methods=["GET", "POST"])
def checkouttest():
    cart_items = get_cart()
    total = calculate_total(cart_items)

    if request.method == "POST":
        email = request.form.get("email")
        zahlart = request.form.get("zahlart", "paypal")  # ← NEU

        if not email or not cart_items:
            flash("Bitte gültige Daten eingeben.", "error")
            return redirect(url_for("checkout"))

        # Session speichern
        session["checkout_email"] = email
        session["checkout_vorname"] = request.form.get("vorname")
        session["checkout_nachname"] = request.form.get("nachname")
        session["checkout_strasse"] = request.form.get("strasse")
        session["checkout_hausnummer"] = request.form.get("hausnummer")
        session["checkout_plz"] = request.form.get("plz")
        session["checkout_stadt"] = request.form.get("stadt")
        session["checkout_land"] = request.form.get("land")
        session["checkout_telefon"] = request.form.get("telefon")
        session["checkout_zahlart"] = zahlart  # ← NEU

        # ── RECHNUNG: sofort Bestellung anlegen ──────────────────
        if zahlart == "rechnung":
            bestellung = Bestellung(
                email=email,
                vorname=session.get("checkout_vorname"),
                nachname=session.get("checkout_nachname"),
                strasse=session.get("checkout_strasse"),
                hausnummer=session.get("checkout_hausnummer"),
                plz=session.get("checkout_plz"),
                stadt=session.get("checkout_stadt"),
                land=session.get("checkout_land"),
                telefon=session.get("checkout_telefon"),
                paymentmethod="rechnung"
            )
            db.session.add(bestellung)
            db.session.flush()

            for item in cart_items:
                db.session.add(BestellPosition(
                    bestellung_id=bestellung.id,
                    bezeichnung=item["title"],
                    menge=item["quantity"],
                    preis=item["price"]
                ))

            # Gutschein anwenden
            gutschein_session = session.get("gutschein")
            if gutschein_session:
                gutschein = Gutschein.query.filter_by(
                    code=gutschein_session["code"]
                ).first()
                if gutschein and gutschein.ist_gueltig():
                    gutschein.restwert -= float(gutschein_session["betrag"])
                    if gutschein.restwert <= 0:
                        gutschein.restwert = 0
                        gutschein.aktiv = False

            db.session.commit()

       

            # An Buchbutler senden (mol_zahlart_id=2 = Rechnung)
            buch_items = [i for i in cart_items if not i.get("is_gutschein") and i.get("ean")]
            if buch_items:
                try:
                    sende_bestellung_an_buchbutler(bestellung, buch_items)
                except Exception:
                    logger.exception("BuchButler Rechnung fehlgeschlagen")

            # Bestätigungsmail
            try:
                send_email(
                    subject="Deine Bestellung bei ibk-bilderbuch.de",
                    recipient=email,
                    html=f"<p>Vielen Dank für deine Bestellung #{bestellung.id}!<br>Zahlungsart: Rechnung</p>"
                )
            except Exception:
                logger.exception("Bestätigungsmail fehlgeschlagen")

            session.pop("cart", None)
            session.pop("gutschein", None)

            return redirect(url_for("bestelldanke"))

        # ── PAYPAL: nur Session speichern, JS übernimmt ──────────
        # render_template unten zeigt PayPal-Button

    return render_template(
        "checkouttest.html",
        cart_items=cart_items,
        total=total
    )


@app.route("/free-checkout", methods=["POST"])
@csrf.exempt
def free_checkout():
    cart_items = get_cart()

    if not cart_items:
        return jsonify({"status": "error", "message": "Warenkorb leer"}), 400

    bestellung = Bestellung(
        email=session.get("checkout_email"),
        vorname=session.get("checkout_vorname"),
        nachname=session.get("checkout_nachname"),
        strasse=session.get("checkout_strasse"),
        hausnummer=session.get("checkout_hausnummer"),
        plz=session.get("checkout_plz"),
        stadt=session.get("checkout_stadt"),
        land=session.get("checkout_land"),
        telefon=session.get("checkout_telefon"),
        paymentmethod="gutschein"
    )
    db.session.add(bestellung)
    db.session.flush()

    # Gutschein abbuchen
    gutschein_session = session.get("gutschein")
    if gutschein_session:
        gutschein = Gutschein.query.filter_by(
            code=gutschein_session["code"]
        ).first()
        if gutschein and gutschein.ist_gueltig():
            gutschein.restwert -= float(gutschein_session["betrag"])
            if gutschein.restwert <= 0:
                gutschein.restwert = 0
                gutschein.aktiv = False

    # Positionen speichern
    for item in cart_items:
        db.session.add(BestellPosition(
            bestellung_id=bestellung.id,
            bezeichnung=item["title"],
            menge=item["quantity"],
            preis=item["price"]
        ))

    db.session.commit()

    # Bücher an BuchButler
    buch_items = [
        item for item in cart_items
        if not item.get("is_gutschein") and item.get("ean")
    ]
    if buch_items:
        try:
            sende_bestellung_an_buchbutler(bestellung, buch_items)
        except Exception:
            logger.exception("BuchButler free-checkout fehlgeschlagen")

    session.pop("cart", None)
    session.pop("gutschein", None)

    return jsonify({"status": "success"})
# =====================================================
# HILFSFUNKTIONEN CART
# =====================================================

def get_cart():
    return session.get("cart", [])

def save_cart(cart):
    session["cart"] = cart
    session.modified = True




def check_auth():
    if not BUCHBUTLER_USER or not BUCHBUTLER_PASSWORD:
        logger.error("Buchbutler Zugangsdaten fehlen")
        return False
    return True


def to_float(value):
    """Konvertiert API Preis sicher"""
    if not value:
        return 0.0
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return 0.0


def to_int(value):
    """Konvertiert Zahlen sicher"""
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def attr(attrs, key):
    """Greift sicher auf Artikelattribute zu"""
    return (attrs.get(key) or {}).get("Wert", "")


def ist_ausverkauft(handling_zeit, schwelle=998):
    """BuchButler liefert ab ~998 Werktage als Signal für 'nicht lieferbar'"""
    try:
        return int(handling_zeit) >= schwelle
    except (TypeError, ValueError):
        return False

def buchbutler_request(endpoint, ean):
    """Allgemeine Request Funktion"""
    url = f"{BASE_URL}/{endpoint}/"

    params = {
        "username": BUCHBUTLER_USER,
        "passwort": BUCHBUTLER_PASSWORD,
        "ean": ean
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()

    if not data or "response" not in data:
        return None

    return data["response"]



# Oben bei den Imports in app.py ergänzen (falls noch nicht vorhanden):
# from werkzeug.security import generate_password_hash, check_password_hash


# =====================================================
# WORKSHOP
# =====================================================




@app.route("/workshop/<workshop_ref>")
def workshop_detail(workshop_ref):

    # Falls jemand noch eine alte Zahlen-URL aufruft (z.B. /workshop/1),
    # automatisch auf die neue, lesbare Slug-URL weiterleiten
    if workshop_ref.isdigit():
        workshop = Workshop.query.get_or_404(int(workshop_ref))
        return redirect(url_for("workshop_detail", workshop_ref=workshop.slug), code=301)

    workshop = Workshop.query.filter_by(slug=workshop_ref).first_or_404()
    slots = [s for s in workshop.slots if s.ist_buchbar()]
    return render_template("workshop.html", workshop=workshop, slots=slots)


# Workshop buchen — PayPal Order erstellen
@app.route("/workshop/<int:slot_id>/create-order", methods=["POST"])
@csrf.exempt
def workshop_create_order(slot_id):
    slot = WorkshopSlot.query.get_or_404(slot_id)

    if not slot.ist_buchbar():
        return jsonify({"error": "Slot nicht mehr buchbar"}), 400

    access_token = paypal_access_token()

    response = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": "EUR",
                    "value": f"{slot.workshop.preis:.2f}"
                }
            }]
        }
    )

    order_data = response.json()

    if "id" not in order_data:
        return jsonify({"error": "PayPal Fehler", "details": order_data}), 400

    # Slot-ID in Session merken
    session["workshop_slot_id"] = slot_id
    session["workshop_name"] = request.json.get("name")
    session["workshop_email"] = request.json.get("email")

    return jsonify({"id": order_data["id"]})


# Workshop buchen — PayPal Capture
@app.route("/workshop/capture/<order_id>", methods=["POST"])
@csrf.exempt
def workshop_capture(order_id):
    try:
        access_token = paypal_access_token()

        response = requests.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
        )

        data = response.json()

        if data.get("status") != "COMPLETED":
            return jsonify({"status": "error", "message": "Zahlung nicht abgeschlossen"}), 400

        slot_id = session.get("workshop_slot_id")
        name = session.get("workshop_name")
        email = session.get("workshop_email")

        if not slot_id or not email:
            return jsonify({"status": "error", "message": "Session abgelaufen"}), 400

        slot = WorkshopSlot.query.get_or_404(slot_id)

        if not slot.ist_buchbar():
            return jsonify({"status": "error", "message": "Slot nicht mehr verfügbar"}), 400

        # Buchung speichern
        buchung = WorkshopBuchung(
            slot_id=slot_id,
            name=name,
            email=email,
            paypal_order_id=order_id
        )
        db.session.add(buchung)

        # Platz abziehen
        slot.plaetze_frei -= 1

        db.session.commit()

        # Bestätigungsmail
        try:
            send_email(
                subject=f"Buchungsbestätigung: {slot.workshop.titel}",
                recipient=email,
                html=f"""
                <p>Hallo {name},</p>
                <p>deine Buchung für <strong>{slot.workshop.titel}</strong> am
                <strong>{slot.datum.strftime('%d.%m.%Y %H:%M')} Uhr</strong>
                ist bestätigt!</p>
                <p>Wir freuen uns auf dich.</p>
                """
            )
        except Exception:
            logger.exception("Workshop Bestätigungsmail fehlgeschlagen")

        # Session aufräumen
        session.pop("workshop_slot_id", None)
        session.pop("workshop_name", None)
        session.pop("workshop_email", None)

        return jsonify({"status": "success"})

    except Exception as e:
        logger.exception("Workshop Capture Fehler")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── ADMIN ──────────────────────────────────────────


# Hilfsfunktion: eindeutigen Slug aus dem Titel erzeugen
def erzeuge_eindeutigen_workshop_slug(titel):
    basis_slug = slugify(titel)
    slug = basis_slug
    zaehler = 2

    while Workshop.query.filter_by(slug=slug).first():
        slug = f"{basis_slug}-{zaehler}"
        zaehler += 1

    return slug


# Hilfsfunktion: darf die aktuelle Person diesen Workshop bearbeiten?
# (Superadmin ODER hat sich mit dem Workshop-Passwort für genau diesen
#  Workshop freigeschaltet)
def workshop_zugriff_ok(workshop_id):
    return session.get("admin") or session.get(f"ws_zugriff_{workshop_id}")


@app.route("/admin/workshops")
def admin_workshops():
    if not session.get("admin"):
        abort(403)
    workshops = Workshop.query.order_by(Workshop.erstellt_am.desc()).all()
    return render_template("admin_workshops.html", workshops=workshops)


@app.route("/admin/workshop/neu", methods=["GET", "POST"])
@csrf.exempt
def admin_workshop_neu():
    if not session.get("admin"):
        abort(403)

    if request.method == "POST":
        titel = request.form.get("titel")
        verwalter_passwort = request.form.get("verwalter_passwort")

        workshop = Workshop(
            titel=titel,
            beschreibung=request.form.get("beschreibung"),
            bild=request.form.get("bild"),
            preis=float(request.form.get("preis", 0)),
            aktiv=True,
            slug=erzeuge_eindeutigen_workshop_slug(titel)
        )

        if verwalter_passwort:
            workshop.verwalter_passwort_hash = generate_password_hash(verwalter_passwort)

        db.session.add(workshop)
        db.session.commit()

        return redirect(f"/admin/workshop/{workshop.slug}")

    return render_template("admin_workshop_neu.html")


@app.route("/admin/workshop/<slug>", methods=["GET", "POST"])
@csrf.exempt
def admin_workshop_detail(slug):
    workshop = Workshop.query.filter_by(slug=slug).first_or_404()

    ist_superadmin = session.get("admin")
    schon_freigeschaltet = session.get(f"ws_zugriff_{workshop.id}")

    if not ist_superadmin and not schon_freigeschaltet:

        if request.method == "POST":
            eingabe = request.form.get("verwalter_passwort", "")

            if workshop.verwalter_passwort_hash and check_password_hash(
                workshop.verwalter_passwort_hash, eingabe
            ):
                session[f"ws_zugriff_{workshop.id}"] = True
            else:
                flash("Falsches Passwort.", "error")
                return render_template("workshop_passwort_abfrage.html", workshop=workshop)
        else:
            return render_template("workshop_passwort_abfrage.html", workshop=workshop)

    return render_template("admin_workshop_detail.html", workshop=workshop)


@app.route("/admin/workshop/<int:workshop_id>/slot/neu", methods=["POST"])
@csrf.exempt
def admin_slot_neu(workshop_id):
    if not workshop_zugriff_ok(workshop_id):
        abort(403)

    workshop = Workshop.query.get_or_404(workshop_id)

    from datetime import datetime
    datum_str = request.form.get("datum")
    plaetze = int(request.form.get("plaetze", 10))

    datum = datetime.strptime(datum_str, "%Y-%m-%dT%H:%M")

    slot = WorkshopSlot(
        workshop_id=workshop_id,
        datum=datum,
        plaetze_gesamt=plaetze,
        plaetze_frei=plaetze,
        aktiv=True
    )
    db.session.add(slot)
    db.session.commit()

    return redirect(f"/admin/workshop/{workshop.slug}")


@app.route("/admin/slot/<int:slot_id>/deaktivieren", methods=["POST"])
@csrf.exempt
def admin_slot_deaktivieren(slot_id):
    slot = WorkshopSlot.query.get_or_404(slot_id)

    if not workshop_zugriff_ok(slot.workshop_id):
        abort(403)

    slot.aktiv = False
    db.session.commit()
    return redirect(f"/admin/workshop/{slot.workshop.slug}")


@app.route("/workshops")
def workshops_overview():
    workshops = Workshop.query.filter_by(aktiv=True).order_by(Workshop.erstellt_am.desc()).all()
    return render_template("workshops.html", workshops=workshops)
# ============================
# KONTAKT
# ============================

@app.route("/kontakt")  
def kontakt():
    return render_template("kontakt.html", user_email=session.get("user_email"))

@app.route("/submit", methods=["POST"])
@csrf.exempt
def submit():
    name = request.form.get("name")
    email = request.form.get("email")
    message = request.form.get("message")
    if not name or not email or not message:
        flash("Bitte fülle alle Felder aus!", "error")
        return redirect("/kontakt")
    try:
        send_email(
            subject=f"Neue Nachricht von {name}",
            recipient=EMAIL_SENDER,
            html=f"""
                <p><b>Von:</b> {name} ({email})</p>
                <p>{message}</p>
            """,
            plain_text=f"Von: {name} <{email}>\n\n{message}"
        )
        flash("Danke! Deine Nachricht wurde gesendet.", "success")
    except Exception as e:
        flash(f"Fehler beim Senden: {e}", "error")
    return redirect("/kontaktdanke")


# ============================
# WIderruf
# ============================

@app.route("/widerruf")  
def widerruf():
    return render_template("widerruf.html", user_email=session.get("user_email"))

@app.route("/submitwiderruf", methods=["POST"])
@csrf.exempt
def submitwiderruf():
    name = request.form.get("name")
    email = request.form.get("email")
    message = request.form.get("message")

    anschrift = request.form.get("anschrift")
    anzahl = request.form.get("anzahl")
    warenbezeichnung = request.form.get("warenbezeichnung")
    datum = request.form.get("datum")
    if not name or not email or not anschrift or not anzahl or not warenbezeichnung or not datum:
        flash("Bitte fülle alle Felder aus!", "error")
        return redirect("/widerruf")
    try:
        send_email(
            subject=f"Neue Nachricht von {name}",
            recipient=EMAIL_SENDER,
            html=f"""
                <p><b>Von:</b> {name} ({email})</p>
                
                <p><b>anschrift:</b> {anschrift}</p>
                <p><b>anzahl:</b> {anzahl}</p>
                <p><b>warenbezeichnung:</b> {warenbezeichnung}</p>
                <p><b>datum:</b> {datum}</p>
                
                <p>{message}</p>
            """,
            plain_text=f"Von: {name} <{email}>\n\n{message}"
        )
        flash("Danke! Deine Nachricht wurde gesendet.", "success")
    except Exception as e:
        flash(f"Fehler beim Senden: {e}", "error")
    return redirect("/kontaktdanke")

# ============================
# NEWSLETTER
# ============================


def send_email(subject, recipient, html, plain_text=None):
    if not SENDGRID_API_KEY or not EMAIL_SENDER:
        logger.warning("SendGrid nicht konfiguriert")
        return

    message = Mail(
        from_email=EMAIL_SENDER,
        to_emails=recipient,
        subject=subject,
        html_content=html,
        plain_text_content=plain_text
    )

    sg = SendGridAPIClient(SENDGRID_API_KEY)

    try:
        sg.send(message)
    except Exception:
        logger.exception("Email Versand fehlgeschlagen")


@app.route("/admin/newsletter")
def admin_newsletter():
    if not session.get("admin"):
        abort(403)

    subscribers = NewsletterSubscriber.query.order_by(
        NewsletterSubscriber.created_at.desc()
    ).all()

    return render_template(
        "admin_newsletter.html",
        subscribers=subscribers
    )


@app.route("/newsletter", methods=["POST"])
def newsletter():
    email = request.form.get("email")

    # Honeypot-Check — Bots füllen dieses Feld aus, Menschen sehen es nicht
    if request.form.get("website"):
        logger.info(f"Newsletter Honeypot ausgelöst: IP={get_remote_address()}")
        flash("Bitte bestätige deine Anmeldung per E-Mail.", "success")
        return redirect("/newsletterbesteatigung")

    if not email:
        flash("Bitte gib eine gültige E-Mail-Adresse ein.", "error")
        return redirect("/")

    # Prüfen ob schon vorhanden
    existing = NewsletterSubscriber.query.filter_by(email=email).first()
    if existing:
        flash("Du bist bereits angemeldet.", "info")
        return redirect("/")

    # Token erzeugen
    token = str(uuid.uuid4())

    subscriber = NewsletterSubscriber(
        email=email,
        token=token,
        confirmed=False
    )

    db.session.add(subscriber)
    db.session.commit()

    # Bestätigungslink
    confirm_url = url_for("confirm_newsletter", token=token, _external=True)
    # HTML-Mail
    html_body = f"""
    <div style="font-family: Arial, sans-serif; text-align: center; padding: 20px;">
        <h2>Newsletter bestätigen</h2>
        <p>Danke für deine Anmeldung!</p>
        <p>Klicke auf den Button, um deine E-Mail zu bestätigen:</p>

        <a href="{confirm_url}" 
           style="
               display: inline-block;
               padding: 12px 20px;
               background-color: #7393B3;
               color: white;
               text-decoration: none;
               border-radius: 6px;
               font-weight: bold;
           ">
           Jetzt bestätigen
        </a>
    </div>
    """

    send_email(
        subject="Bitte bestätige deine Newsletter-Anmeldung",
        recipient=email,
        html=html_body
    )

    flash("Bitte bestätige deine Anmeldung per E-Mail.", "success")
    return redirect("/newsletterbesteatigung")


@app.route("/newsletter/confirm/<token>")
def confirm_newsletter(token):
    subscriber = NewsletterSubscriber.query.filter_by(token=token).first()

    if not subscriber:
        flash("Ungültiger Bestätigungslink.", "error")
        return redirect("/")

    subscriber.confirmed = True
    subscriber.token = None
    db.session.commit()

    flash("Newsletter erfolgreich bestätigt 🎉", "success")
    return redirect("/danke")


@app.route("/admin/send-newsletter", methods=["POST"])
def send_newsletter():
    if not session.get("admin"):
        abort(403)

    subject = request.form.get("subject")
    content = request.form.get("content")  # HTML erlaubt

    subscribers = NewsletterSubscriber.query.filter_by(confirmed=True).all()

    for sub in subscribers:
        unsubscribe_url = url_for(
            "unsubscribe_newsletter",
            token=sub.token,
            _external=True
        )

        html_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px;">
            {content}

            <p style="margin-top:20px; font-size:12px; color: gray;">
                <a href="{unsubscribe_url}">Abmelden vom Newsletter</a>
            </p>
        </div>
        """

        send_email(
            subject=subject,
            recipient=sub.email,
            html=html_body
        )

    return f"{len(subscribers)} Emails gesendet ✅"


@app.route("/newsletter/unsubscribe/<token>")
def unsubscribe_newsletter(token):
    subscriber = NewsletterSubscriber.query.filter_by(token=token).first()

    if not subscriber:
        flash("Ungültiger Abmeldelink.", "error")
        return redirect("/")

    db.session.delete(subscriber)
    db.session.commit()

    flash("Du hast dich erfolgreich vom Newsletter abgemeldet.", "success")
    return redirect("/")
# ============================
# RECHTLICHES
# ============================

@app.route("/agb")
def agb():
    return render_template("agb.html", user_email=session.get("user_email"))

@app.route("/datenschutz")
def datenschutz():
    return render_template("datenschutz.html", user_email=session.get("user_email"))

@app.route("/impressum")
def impressum():
    return render_template("impressum.html", user_email=session.get("user_email"))



# ============================
# DANKE SEITEN
# ============================

@app.route("/danke")
def danke():
    return render_template("danke.html", user_email=session.get("user_email"))

@app.route("/kontaktdanke")
def kontaktdanke():
    return render_template("kontaktdanke.html", user_email=session.get("user_email"))

@app.route("/bestelldanke")
def bestelldanke():
    return render_template("bestelldanke.html", user_email=session.get("user_email"))

@app.route("/newsletterbesteatigung")
def newsletterbesteatigung():
    return render_template("newsletterbesteatigung.html", user_email=session.get("user_email"))
    
@app.route("/newsletteranmeldung")
def newsletteranmeldung():
    return render_template("newsletteranmeldung.html", user_email=session.get("user_email"))

@app.route("/illustratoren")
def illustratoren():
    return render_template("illustratoren.html", user_email=session.get("user_email"))

@app.route("/ueberibk")
def ueberibk():
    return render_template("ueberibk.html", user_email=session.get("user_email"))

    
@app.route("/uberibk")
def uberibk():
    return render_template("uberibk.html", user_email=session.get("user_email"))
    
# ============================
# INDEX HAUPTSEITE
# ============================

@app.route("/")
def index():

    kategorienamen = [
      "andere" ]

    kategorie_beschreibungen = {
        "Jacominus Gainsborough": {
            "kurz": "Einer, der sich erinnert. Und manchmal auch vergisst.",
            "lang": "Jacominus sitzt im Garten, denkt nach, lauscht dem Wind. Eine Erinnerung streift ihn – kaum greifbar, wie ein Traum, der sich beim Aufwachen auflöst. Und doch ist da etwas, das bleibt: ein Gefühl, warm und vertraut. Es sind die winzig kleinen Sekunden, die zählen. Die kaum sichtbaren Augenblicke zwischen zwei Herzschlägen, in denen sich alles entscheiden kann. Ein Blick. Ein Lächeln. Ein Wiedersehen. Und irgendwo ist immer jemand unterwegs. Über Wiesen, durch Straßen, vorbei an flüchtigen Begegnungen. Schritt für Schritt, einer Verabredung entgegen. Vielleicht Punkt zwölf. Vielleicht genau im richtigen Moment. So entfaltet sich ein Leben – nicht laut und in Bildern und Worten, die bleiben. In Begegnungen, die alles verändern können. Kein außergewöhnliches Leben. Und doch ein ganz besonderes. Das Leben von Jacominus Gainsborough"
        },

        "Mut oder Angst?!": {
            "kurz": "Mut ist etwas Gutes!",
            "lang": "Wer sein Herz in die Hand nimmt und das Zaudern überwindet, auf den wartet ein ganz besonderes Hochgefühl – mal leise, mal kraftvoll, aber immer spürbar. Davon erzählen diese Bücher auf ganz unterschiedliche Weise.\nDa sind die poetischen Bilder und Gedichte, die genau diesen Moment einfangen, in dem aus Zögern plötzlich Mut wird. Da ist Anna, die ihre Angst nicht einfach wegschiebt, sondern ihr mit Fantasie, Freundschaft und liebevoller Unterstützung begegnet – mit Riesen, Rittern und vielen anderen Begleitern durch die Nacht.\nUnd da ist der Blickwechsel: Was, wenn Angst ganz anders verstanden wird? Wenn aus dem „Angsthasen“ ein „Muthase“ wird – und plötzlich Mut, Klugheit und Sensibilität sichtbar werden, wo vorher vorschnell bewertet wurde?\nNicht zuletzt erzählen die Geschichten auch davon, wie man mit Veränderungen umgeht, neue Wege findet und den eigenen Platz behauptet – manchmal leise, manchmal überraschend.\n\nBücher für Kinder, ja – aber eigentlich für alle, die sich erinnern möchten, wie sich Mut anfühlt."
    }
}
   

    kategorien = [
        (k, [p for p in produkte if k in p.get("kategorie", [])])
        for k in kategorienamen
    ]

    # leere Kategorien entfernen
    kategorien = [
        (titel, liste)
        for titel, liste in kategorien
        if liste
    ]

    return render_template(
        "index.html",
        kategorien=kategorien,
        kategorie_beschreibungen=kategorie_beschreibungen,
        user_email=session.get("user_email")
    )
    



    
# =====================================================
# START (RENDER READY)
# =====================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
