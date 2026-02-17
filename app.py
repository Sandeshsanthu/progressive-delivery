from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from functools import wraps

import mysql.connector
from mysql.connector import errors as mysql_errors
from mysql.connector import Error, errorcode
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

from feature_flags import init_unleash, flag

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")  # set env var in real use

# Initialize Unleash (recommended once at startup)
init_unleash()

# ----------------------------
# Database helpers (MySQL)
# ----------------------------
def get_db():
    if "db" not in g:
        g.db = mysql.connector.connect(
            host=os.getenv("DB_HOST", "db"),  # IMPORTANT: default to Docker service name
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", "app"),
            password=os.getenv("DB_PASSWORD", "apppass"),
            database=os.getenv("DB_NAME", "progressive_delivery"),
            autocommit=False,  # we manage commits explicitly
        )
    return g.db


def query_all(sql, params=None):
    cnx = get_db()
    cur = cnx.cursor(dictionary=True)
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    return rows


def query_one(sql, params=None):
    cnx = get_db()
    cur = cnx.cursor(dictionary=True)
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


def exec_sql(sql, params=None):
    cnx = get_db()
    cur = cnx.cursor()
    cur.execute(sql, params or ())
    last_id = cur.lastrowid
    cur.close()
    return last_id


def create_index_if_missing(cur, ddl: str) -> None:
    """
    MySQL 8.0 does NOT support: CREATE INDEX IF NOT EXISTS ...
    So we attempt CREATE INDEX and ignore "duplicate key name" (1061).
    """
    try:
        cur.execute(ddl)
    except Error as err:
        # 1061 = duplicate key name (index already exists)
        if err.errno != errorcode.ER_DUP_KEYNAME:
            raise


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def init_db():
    cnx = get_db()
    cur = cnx.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INT PRIMARY KEY AUTO_INCREMENT,
            name VARCHAR(255) NOT NULL,
            email VARCHAR(255) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            created_at VARCHAR(32) NOT NULL
        ) ENGINE=InnoDB;
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INT PRIMARY KEY AUTO_INCREMENT,
            user_id INT NOT NULL,
            title VARCHAR(255) NOT NULL,
            make VARCHAR(100) NOT NULL,
            model VARCHAR(100) NOT NULL,
            `year` INT NOT NULL,
            mileage INT NOT NULL,
            price_cents INT NOT NULL,
            currency VARCHAR(8) NOT NULL DEFAULT 'USD',
            location VARCHAR(255) NOT NULL,
            description TEXT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE or SOLD
            created_at VARCHAR(32) NOT NULL,
            updated_at VARCHAR(32) NOT NULL,
            CONSTRAINT fk_listings_user FOREIGN KEY (user_id)
                REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB;
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INT PRIMARY KEY AUTO_INCREMENT,
            created_at VARCHAR(32) NOT NULL,
            total_amount_cents INT NOT NULL,
            payment_ref VARCHAR(64) NOT NULL
        ) ENGINE=InnoDB;
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS purchase_items (
            id INT PRIMARY KEY AUTO_INCREMENT,
            purchase_id INT NOT NULL,
            listing_id INT NOT NULL,
            price_cents INT NOT NULL,
            CONSTRAINT fk_pi_purchase FOREIGN KEY (purchase_id)
                REFERENCES purchases(id) ON DELETE CASCADE,
            CONSTRAINT fk_pi_listing FOREIGN KEY (listing_id)
                REFERENCES listings(id) ON DELETE RESTRICT
        ) ENGINE=InnoDB;
        """
    )

    # Indexes (safe for repeated runs)
    create_index_if_missing(cur, "CREATE INDEX idx_listings_status_created ON listings(status, created_at)")
    create_index_if_missing(cur, "CREATE INDEX idx_listings_user ON listings(user_id, created_at)")
    create_index_if_missing(cur, "CREATE INDEX idx_purchase_items_purchase ON purchase_items(purchase_id)")
    create_index_if_missing(cur, "CREATE INDEX idx_purchase_items_listing ON purchase_items(listing_id)")

    cnx.commit()
    cur.close()


_db_inited = False


@app.before_request
def _ensure_db():
    # NOTE: For Kubernetes, prefer a one-time migration Job.
    global _db_inited
    if not _db_inited:
        init_db()
        _db_inited = True


# ----------------------------
# Template context
# ----------------------------
@app.context_processor
def inject_feature_flags():
    return {
        "CART_ENABLED": flag("cart.enabled", default=True),
        "CHECKOUT_ENABLED": flag("checkout.enabled", default=True),
        "PAYMENT_DUMMY_ENABLED": flag("payment.dummy.enabled", default=True),
    }


# ----------------------------
# Auth utilities
# ----------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return query_one("SELECT id, name, email FROM users WHERE id = %s", (int(uid),))


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# ----------------------------
# Validation helpers
# ----------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_int(value: str, field: str, min_v: int | None = None, max_v: int | None = None) -> int:
    try:
        n = int(value)
    except Exception:
        raise ValueError(f"{field} must be a whole number.")
    if min_v is not None and n < min_v:
        raise ValueError(f"{field} must be at least {min_v}.")
    if max_v is not None and n > max_v:
        raise ValueError(f"{field} must be at most {max_v}.")
    return n


def parse_price_to_cents(value: str) -> int:
    v = value.strip().replace(",", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", v):
        raise ValueError("Price must be a number like 12000 or 12000.50.")
    if "." in v:
        dollars, cents = v.split(".")
        cents = (cents + "0")[:2]
    else:
        dollars, cents = v, "00"
    price_cents = int(dollars) * 100 + int(cents)
    if price_cents <= 0:
        raise ValueError("Price must be greater than 0.")
    if price_cents > 500_000_000 * 100:
        raise ValueError("Price looks too large.")
    return price_cents


def money_display(price_cents: int, currency: str = "USD") -> str:
    return f"{currency} {price_cents/100:,.2f}"


@app.template_filter("money")
def _money_filter(price_cents):
    return money_display(int(price_cents))


# ----------------------------
# Routes: Public
# ----------------------------
@app.get("/")
def index():
    q = (request.args.get("q") or "").strip()
    make = (request.args.get("make") or "").strip()
    min_year = (request.args.get("min_year") or "").strip()
    max_price = (request.args.get("max_price") or "").strip()

    where = ["l.status = 'ACTIVE'"]
    params: list = []

    if q:
        where.append("(l.title LIKE %s OR l.description LIKE %s OR l.model LIKE %s OR l.make LIKE %s OR l.location LIKE %s)")
        like = f"%{q}%"
        params += [like, like, like, like, like]

    if make:
        where.append("l.make LIKE %s")
        params.append(f"%{make}%")

    if min_year:
        try:
            y = parse_int(min_year, "Min year", 1900, datetime.now().year + 1)
            where.append("l.`year` >= %s")
            params.append(y)
        except ValueError as e:
            flash(str(e), "warning")

    if max_price:
        try:
            max_cents = parse_price_to_cents(max_price)
            where.append("l.price_cents <= %s")
            params.append(max_cents)
        except ValueError as e:
            flash(str(e), "warning")

    sql = f"""
        SELECT l.*, u.name AS seller_name
        FROM listings l
        JOIN users u ON u.id = l.user_id
        WHERE {' AND '.join(where)}
        ORDER BY l.created_at DESC
        LIMIT 50;
    """
    listings = query_all(sql, tuple(params))
    return render_template("index.html", listings=listings, q=q, make=make, min_year=min_year, max_price=max_price)


@app.get("/listing/<int:listing_id>")
def listing_detail(listing_id: int):
    listing = query_one(
        """
        SELECT l.*, u.name AS seller_name, u.email AS seller_email
        FROM listings l
        JOIN users u ON u.id = l.user_id
        WHERE l.id = %s
        """,
        (listing_id,),
    )
    if not listing:
        abort(404)
    return render_template("listing_detail.html", listing=listing)


# ----------------------------
# Routes: Auth
# ----------------------------
@app.get("/register")
def register():
    return render_template("register.html")


@app.post("/register")
def register_post():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if len(name) < 2:
        flash("Name must be at least 2 characters.", "danger")
        return redirect(url_for("register"))
    if not EMAIL_RE.match(email):
        flash("Please enter a valid email.", "danger")
        return redirect(url_for("register"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("register"))

    cnx = get_db()
    try:
        exec_sql(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (%s, %s, %s, %s)",
            (name, email, generate_password_hash(password), utc_now_iso()),
        )
        cnx.commit()
    except mysql_errors.IntegrityError:
        cnx.rollback()
        flash("That email is already registered. Please log in.", "warning")
        return redirect(url_for("login"))

    flash("Account created. Please log in.", "success")
    return redirect(url_for("login"))


@app.get("/login")
def login():
    return render_template("login.html", next=request.args.get("next") or "")


@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    next_url = (request.form.get("next") or "").strip() or url_for("index")

    user = query_one("SELECT * FROM users WHERE email = %s", (email,))
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Invalid email or password.", "danger")
        return redirect(url_for("login", next=next_url))

    session.clear()
    session["user_id"] = int(user["id"])
    flash(f"Welcome back, {user['name']}!", "success")
    return redirect(next_url)


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# ----------------------------
# Routes: Listings (seller actions)
# ----------------------------
@app.get("/listings/new")
@login_required
def new_listing():
    return render_template("new_listing.html")


@app.post("/listings/new")
@login_required
def new_listing_post():
    user = current_user()
    assert user is not None

    title = (request.form.get("title") or "").strip()
    make = (request.form.get("make") or "").strip()
    model = (request.form.get("model") or "").strip()
    year_s = (request.form.get("year") or "").strip()
    mileage_s = (request.form.get("mileage") or "").strip()
    price_s = (request.form.get("price") or "").strip()
    location = (request.form.get("location") or "").strip()
    description = (request.form.get("description") or "").strip()

    errors = []
    if len(title) < 5:
        errors.append("Title must be at least 5 characters.")
    if not make:
        errors.append("Make is required.")
    if not model:
        errors.append("Model is required.")
    if len(location) < 2:
        errors.append("Location is required.")
    if len(description) < 20:
        errors.append("Description must be at least 20 characters (add details).")

    try:
        year = parse_int(year_s, "Year", 1900, datetime.now().year + 1)
    except ValueError as e:
        errors.append(str(e))
        year = 2000

    try:
        mileage = parse_int(mileage_s, "Mileage", 0, 2_000_000)
    except ValueError as e:
        errors.append(str(e))
        mileage = 0

    try:
        price_cents = parse_price_to_cents(price_s)
    except ValueError as e:
        errors.append(str(e))
        price_cents = 0

    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("new_listing"))

    now = utc_now_iso()
    cnx = get_db()
    exec_sql(
        """
        INSERT INTO listings
          (user_id, title, make, model, `year`, mileage, price_cents, currency, location, description, status, created_at, updated_at)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, 'USD', %s, %s, 'ACTIVE', %s, %s)
        """,
        (int(user["id"]), title, make, model, year, mileage, price_cents, location, description, now, now),
    )
    cnx.commit()

    flash("Your car has been posted and is now visible to buyers.", "success")
    return redirect(url_for("my_listings"))


@app.get("/me/listings")
@login_required
def my_listings():
    user = current_user()
    assert user is not None
    listings = query_all(
        "SELECT * FROM listings WHERE user_id = %s ORDER BY created_at DESC",
        (int(user["id"]),),
    )
    return render_template("my_listings.html", listings=listings)


def _get_listing_owned_or_404(listing_id: int):
    user = current_user()
    assert user is not None
    listing = query_one(
        "SELECT * FROM listings WHERE id = %s AND user_id = %s",
        (listing_id, int(user["id"])),
    )
    if not listing:
        abort(404)
    return listing


@app.get("/listings/<int:listing_id>/edit")
@login_required
def edit_listing(listing_id: int):
    listing = _get_listing_owned_or_404(listing_id)
    return render_template("edit_listing.html", listing=listing)


@app.post("/listings/<int:listing_id>/edit")
@login_required
def edit_listing_post(listing_id: int):
    listing = _get_listing_owned_or_404(listing_id)

    title = (request.form.get("title") or "").strip()
    price_s = (request.form.get("price") or "").strip()
    location = (request.form.get("location") or "").strip()
    description = (request.form.get("description") or "").strip()

    errors = []
    if len(title) < 5:
        errors.append("Title must be at least 5 characters.")
    if len(location) < 2:
        errors.append("Location is required.")
    if len(description) < 20:
        errors.append("Description must be at least 20 characters.")

    try:
        price_cents = parse_price_to_cents(price_s)
    except ValueError as e:
        errors.append(str(e))
        price_cents = int(listing["price_cents"])

    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("edit_listing", listing_id=listing_id))

    cnx = get_db()
    exec_sql(
        """
        UPDATE listings
        SET title = %s, price_cents = %s, location = %s, description = %s, updated_at = %s
        WHERE id = %s AND user_id = %s
        """,
        (title, price_cents, location, description, utc_now_iso(), listing_id, int(listing["user_id"])),
    )
    cnx.commit()
    flash("Listing updated.", "success")
    return redirect(url_for("my_listings"))


@app.post("/listings/<int:listing_id>/mark-sold")
@login_required
def mark_sold(listing_id: int):
    listing = _get_listing_owned_or_404(listing_id)
    if listing["status"] == "SOLD":
        flash("Listing is already marked SOLD.", "info")
        return redirect(url_for("my_listings"))

    cnx = get_db()
    exec_sql(
        "UPDATE listings SET status = 'SOLD', updated_at = %s WHERE id = %s AND user_id = %s",
        (utc_now_iso(), listing_id, int(listing["user_id"])),
    )
    cnx.commit()
    flash("Listing marked as SOLD.", "success")
    return redirect(url_for("my_listings"))


@app.post("/listings/<int:listing_id>/delete")
@login_required
def delete_listing(listing_id: int):
    listing = _get_listing_owned_or_404(listing_id)

    cnx = get_db()
    exec_sql("DELETE FROM listings WHERE id = %s AND user_id = %s", (listing_id, int(listing["user_id"])))
    cnx.commit()

    flash("Listing deleted.", "success")
    return redirect(url_for("my_listings"))


# ----------------------------
# Cart + Checkout (Feature-flagged)
# ----------------------------
def cart_ids():
    return [int(x) for x in session.get("cart", [])]


def cart_add(listing_id: int):
    ids = cart_ids()
    if listing_id not in ids:
        ids.append(listing_id)
    session["cart"] = ids


def cart_clear():
    session["cart"] = []


def load_cart_items(ids: list[int]):
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    rows = query_all(
        f"SELECT id, title, price_cents, status FROM listings WHERE id IN ({placeholders})",
        tuple(ids),
    )
    by_id = {int(r["id"]): r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def dummy_charge(card_number: str, exp_mm: str, exp_yy: str, cvc: str) -> str:
    if not flag("payment.dummy.enabled", default=True):
        raise ValueError("Dummy payments disabled")
    digits = "".join(ch for ch in (card_number or "") if ch.isdigit())
    if digits != "4242424242424242":
        raise ValueError("Card declined (dummy gateway)")
    if not (exp_mm and exp_yy and cvc):
        raise ValueError("Missing card details")
    return f"DUMMY-{uuid.uuid4().hex[:12].upper()}"


@app.post("/cart/add/<int:listing_id>")
def add_to_cart(listing_id: int):
    if not flag("cart.enabled", default=True):
        abort(404)

    row = query_one("SELECT id, status FROM listings WHERE id = %s", (listing_id,))
    if not row:
        abort(404)
    if row["status"] != "ACTIVE":
        flash("Listing already sold.")
        return redirect(request.referrer or url_for("view_cart"))

    cart_add(listing_id)
    flash("Added to cart.")
    return redirect(request.referrer or url_for("view_cart"))


@app.get("/cart")
def view_cart():
    if not flag("cart.enabled", default=True):
        abort(404)

    ids = cart_ids()
    items = load_cart_items(ids)
    total_cents = sum(int(i["price_cents"]) for i in items if i["status"] == "ACTIVE")
    return render_template(
        "cart.html",
        items=items,
        total_cents=total_cents,
        CHECKOUT_ENABLED=flag("checkout.enabled", default=True),
    )


@app.get("/checkout")
def checkout():
    if not (flag("cart.enabled", True) and flag("checkout.enabled", True)):
        abort(404)

    ids = cart_ids()
    items = load_cart_items(ids)
    if not items:
        flash("Cart is empty.")
        return redirect(url_for("view_cart"))
    if any(i["status"] != "ACTIVE" for i in items):
        flash("One or more listings were sold. Please refresh cart.")
        return redirect(url_for("view_cart"))

    total_cents = sum(int(i["price_cents"]) for i in items)
    return render_template("checkout.html", items=items, total_cents=total_cents)


@app.post("/pay")
def pay():
    if not (flag("cart.enabled", True) and flag("checkout.enabled", True)):
        abort(404)

    card_number = request.form.get("card_number", "")
    exp_mm = request.form.get("exp_mm", "")
    exp_yy = request.form.get("exp_yy", "")
    cvc = request.form.get("cvc", "")

    ids = cart_ids()
    items = load_cart_items(ids)
    if not items:
        flash("Cart is empty.")
        return redirect(url_for("view_cart"))

    cnx = get_db()
    try:
        cnx.start_transaction()

        # Lock the rows to prevent double-sell
        placeholders = ",".join(["%s"] * len(ids))
        locked = query_all(
            f"SELECT id, status, price_cents FROM listings WHERE id IN ({placeholders}) FOR UPDATE",
            tuple(ids),
        )
        by_id = {int(r["id"]): r for r in locked}

        if any((i not in by_id) or (by_id[i]["status"] != "ACTIVE") for i in ids):
            cnx.rollback()
            flash("Some listings were just sold. Try again.")
            return redirect(url_for("view_cart"))

        payment_ref = dummy_charge(card_number, exp_mm, exp_yy, cvc)
        total_cents = sum(int(by_id[i]["price_cents"]) for i in ids)
        now = utc_now_iso()

        purchase_id = exec_sql(
            "INSERT INTO purchases (created_at, total_amount_cents, payment_ref) VALUES (%s, %s, %s)",
            (now, total_cents, payment_ref),
        )

        for i in ids:
            exec_sql(
                "INSERT INTO purchase_items (purchase_id, listing_id, price_cents) VALUES (%s, %s, %s)",
                (purchase_id, i, int(by_id[i]["price_cents"])),
            )

        exec_sql(
            f"UPDATE listings SET status = 'SOLD', updated_at = %s WHERE id IN ({placeholders})",
            tuple([now] + ids),
        )

        cnx.commit()
        cart_clear()
        return redirect(url_for("purchase_success", purchase_id=purchase_id))

    except Exception as e:
        try:
            cnx.rollback()
        except Exception:
            pass
        flash(str(e))
        return redirect(url_for("checkout"))


@app.get("/purchase/<int:purchase_id>/success")
def purchase_success(purchase_id: int):
    p = query_one("SELECT * FROM purchases WHERE id = %s", (purchase_id,))
    if not p:
        abort(404)

    items = query_all(
        """
        SELECT l.id, l.title, pi.price_cents
        FROM purchase_items pi
        JOIN listings l ON l.id = pi.listing_id
        WHERE pi.purchase_id = %s
        """,
        (purchase_id,),
    )
    return render_template("success.html", purchase=p, items=items)


# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
