
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from pathlib import Path
import hashlib, os, uuid, secrets, smtplib, ssl

load_dotenv(Path(__file__).with_name(".env"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

DATABASE_URL = os.getenv("DATABASE_URL")

STAFF_ROLES = {
    "owner": "Owner / Admin",
    "manager": "Manager",
    "agent": "Inquiry Staff / Agent",
    "dropoff_driver": "Drop-Off Driver",
    "mechanic": "Mechanic",
    "guard": "Guard",
    "cleaner": "Cleaner Staff"
}

SERVICE_TASKS = {
    "Maintenance": [
        "Engine Oil Change",
        "Brake Inspection",
        "Brake Cleaning",
        "Tire Inspection",
        "Tire Replacement",
        "Battery Check",
        "Engine Inspection",
        "Fluid Check / Refill",
        "Electrical Check",
        "General Repair"
    ],
    "Cleaning": [
        "Exterior Wash",
        "Interior Vacuum",
        "Interior Wipe Down",
        "Glass Cleaning",
        "Seat Cleaning",
        "Disinfection",
        "Tire Dressing",
        "Full Vehicle Cleaning"
    ],
    "Inspection": [
        "Pre-Rental Inspection",
        "Post-Rental Inspection",
        "Safety Inspection",
        "Damage Inspection",
        "Final Release Inspection"
    ]
}

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

def db():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL in the .env file next to app.py.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

def hash_password(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def money(v):
    try:
        return f"₱{float(v):,.2f}"
    except:
        return v

app.jinja_env.filters["money"] = money

def customer_login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("customer_id"):
            flash("Please log in first to use that feature.", "warning")
            return redirect(url_for("customer_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped

def require_roles(*roles):
    def deco(fn):
        from functools import wraps
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("staff_id"):
                return redirect(url_for("staff_login"))
            if roles and session.get("role") not in roles:
                flash("You do not have access to that module.", "danger")
                return redirect(url_for("staff_dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco

def upload_image(file_obj, folder, prefix):
    if not file_obj or not file_obj.filename:
        return None
    filename = secure_filename(file_obj.filename)
    if "." not in filename:
        raise ValueError("Image file must have a valid extension.")
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Only JPG, JPEG, PNG, and WEBP images are allowed.")
    target_dir = Path(app.root_path) / "static" / "uploads" / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"{prefix}_{uuid.uuid4().hex[:12]}.{ext}"
    file_obj.save(target_dir / final_name)
    return f"uploads/{folder}/{final_name}"

def send_email(to_email, subject, body):
    sender = os.getenv("SMTP_EMAIL", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if not sender or not password:
        print(f"[EMAIL NOT SENT - SMTP NOT CONFIGURED]\nTo: {to_email}\nSubject: {subject}\n{body}")
        return False

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
            smtp.starttls(context=context)
            smtp.login(sender, password)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        print(f"[EMAIL SEND FAILED] {exc}")
        return False

def issue_customer_otp(cur, customer_id, email, purpose):
    otp = f"{secrets.randbelow(1000000):06d}"
    expires = datetime.now() + timedelta(minutes=10)
    cur.execute("""
        UPDATE customers
        SET email_otp_hash=%s, email_otp_expires=%s
        WHERE customer_id=%s
    """, (hash_password(otp), expires, customer_id))

    subject = "Redline Rentals verification code"
    body = (
        f"Your Redline Rentals {purpose} verification code is: {otp}\n\n"
        "This code expires in 10 minutes.\n"
        "If you did not request this code, you can ignore this email."
    )
    sent = send_email(email, subject, body)
    return otp, sent

def inclusive_days(start_at, end_at):
    if isinstance(start_at, str):
        start_at = datetime.fromisoformat(start_at)
    if isinstance(end_at, str):
        end_at = datetime.fromisoformat(end_at)
    if end_at <= start_at:
        raise ValueError("End date/time must be later than the start date/time.")
    seconds = (end_at - start_at).total_seconds()
    return max(1, int((seconds + 86399) // 86400))

def rental_quote(vehicle, start_at, end_at):
    days = inclusive_days(start_at, end_at)
    total = round(float(vehicle["price_per_day"]) * days, 2)
    reservation_payment = round(total * 0.20, 2)
    return {
        "days": days,
        "total": total,
        "reservation_payment": reservation_payment,
        "balance": round(total - reservation_payment, 2)
    }

def finance_period_bounds(period):
    today = date.today()
    if period == "week":
        start = today - timedelta(days=today.weekday())
    elif period == "month":
        start = today.replace(day=1)
    elif period == "year":
        start = today.replace(month=1, day=1)
    else:
        start = today
    return start, today

def finance_summary(cur, period):
    start, end = finance_period_bounds(period)
    cur.execute("SELECT COALESCE(SUM(amount),0) total FROM income_entries WHERE entry_date BETWEEN %s AND %s", (start, end))
    income = float(cur.fetchone()["total"])
    cur.execute("SELECT COALESCE(SUM(amount),0) total FROM expense_entries WHERE expense_date BETWEEN %s AND %s", (start, end))
    operating = float(cur.fetchone()["total"])
    cur.execute("SELECT COALESCE(SUM(amount),0) total FROM payroll WHERE status='Paid' AND paid_date BETWEEN %s AND %s", (start, end))
    wages = float(cur.fetchone()["total"])
    expenses = round(operating + wages, 2)
    return {
        "period": period,
        "start": start,
        "end": end,
        "income": income,
        "expenses": expenses,
        "wages": wages,
        "gross": round(income - expenses, 2)
    }

# ---------------- PUBLIC ----------------

@app.route("/")
def home():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.*,
                COALESCE((SELECT COUNT(*) FROM vehicle_likes l WHERE l.vehicle_id=v.vehicle_id),0) likes,
                COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.vehicle_id=v.vehicle_id),0) avg_rating
                FROM vehicles v
                WHERE v.is_published=1
                ORDER BY v.rental_count DESC, v.created_at DESC
                LIMIT 8
            """)
            vehicles = cur.fetchall()
            cur.execute("SELECT DISTINCT location FROM vehicles WHERE is_published=1 ORDER BY location")
            locations = [r["location"] for r in cur.fetchall()]

            liked = set()
            if session.get("customer_id"):
                cur.execute("SELECT vehicle_id FROM vehicle_likes WHERE customer_id=%s", (session["customer_id"],))
                liked = {r["vehicle_id"] for r in cur.fetchall()}

    for v in vehicles:
        v["liked"] = v["vehicle_id"] in liked
    return render_template("home.html", vehicles=vehicles, locations=locations)

@app.route("/vehicles")
def vehicles():
    q = request.args.get("q", "").strip()
    vehicle_type = request.args.get("type", "").strip()
    location = request.args.get("location", "").strip()
    capacity = request.args.get("capacity", "").strip()

    sql = """
        SELECT v.*,
        COALESCE((SELECT COUNT(*) FROM vehicle_likes l WHERE l.vehicle_id=v.vehicle_id),0) likes,
        COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.vehicle_id=v.vehicle_id),0) avg_rating
        FROM vehicles v
        WHERE v.is_published=1
    """
    params = []
    if q:
        sql += " AND (v.make_model ILIKE %s OR v.category ILIKE %s OR v.location ILIKE %s)"
        like = f"%{q}%"
        params.extend([like, like, like])
    if vehicle_type:
        sql += " AND v.vehicle_type=%s"
        params.append(vehicle_type)
    if location:
        sql += " AND v.location=%s"
        params.append(location)
    if capacity:
        sql += " AND v.passenger_capacity >= %s"
        params.append(int(capacity))
    sql += " ORDER BY CASE v.status WHEN 'Available' THEN 1 WHEN 'On Rent' THEN 2 WHEN 'Maintenance' THEN 3 ELSE 0 END, v.price_per_day"

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.execute("SELECT DISTINCT location FROM vehicles WHERE is_published=1 ORDER BY location")
            locations = [r["location"] for r in cur.fetchall()]

            liked = set()
            if session.get("customer_id"):
                cur.execute("SELECT vehicle_id FROM vehicle_likes WHERE customer_id=%s", (session["customer_id"],))
                liked = {r["vehicle_id"] for r in cur.fetchall()}

    for v in rows:
        v["liked"] = v["vehicle_id"] in liked

    return render_template(
        "vehicles.html",
        vehicles=rows,
        locations=locations,
        q=q,
        vehicle_type=vehicle_type,
        location=location,
        capacity=capacity
    )

@app.route("/vehicle/<int:vehicle_id>")
def vehicle_detail(vehicle_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.*,
                COALESCE((SELECT COUNT(*) FROM vehicle_likes l WHERE l.vehicle_id=v.vehicle_id),0) likes,
                COALESCE((SELECT AVG(r.rating) FROM reviews r WHERE r.vehicle_id=v.vehicle_id),0) avg_rating
                FROM vehicles v
                WHERE v.vehicle_id=%s AND v.is_published=1
            """, (vehicle_id,))
            vehicle = cur.fetchone()
            if not vehicle:
                return "Vehicle not found", 404

            cur.execute("""
                SELECT r.*, c.full_name
                FROM reviews r
                JOIN customers c ON r.customer_id=c.customer_id
                WHERE r.vehicle_id=%s
                ORDER BY r.created_at DESC
            """, (vehicle_id,))
            reviews = cur.fetchall()

            liked = False
            if session.get("customer_id"):
                cur.execute(
                    "SELECT 1 FROM vehicle_likes WHERE customer_id=%s AND vehicle_id=%s",
                    (session["customer_id"], vehicle_id)
                )
                liked = bool(cur.fetchone())

    vehicle["liked"] = liked
    return render_template("vehicle_detail.html", vehicle=vehicle, reviews=reviews)

# ---------------- CUSTOMER AUTH ----------------

@app.route("/register", methods=["GET", "POST"])
def customer_register():
    if request.method == "POST":
        full_name = request.form["full_name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form["phone"].strip()
        password = request.form["password"]

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM customers WHERE email=%s", (email,))
                existing = cur.fetchone()

                if existing and existing["is_verified"]:
                    flash("An account with that email already exists. Please log in.", "warning")
                    return redirect(url_for("customer_login"))

                if existing:
                    cur.execute("""
                        UPDATE customers
                        SET full_name=%s, phone=%s, password_hash=%s
                        WHERE customer_id=%s
                    """, (full_name, phone, hash_password(password), existing["customer_id"]))
                    customer_id = existing["customer_id"]
                else:
                    cur.execute("""
                        INSERT INTO customers(full_name,email,phone,password_hash,is_verified)
                        VALUES(%s,%s,%s,%s,0)
                        RETURNING customer_id
                    """, (full_name, email, phone, hash_password(password)))
                    customer_id = cur.fetchone()["customer_id"]

                otp, sent = issue_customer_otp(cur, customer_id, email, "registration")

        session["pending_register_customer_id"] = customer_id
        if sent:
            flash("A verification code was sent to your Gmail/email account.", "success")
        else:
            flash(f"SMTP is not configured. Development OTP: {otp}", "warning")
        return redirect(url_for("customer_register_verify"))

    return render_template("customer_register.html")

@app.route("/register/verify", methods=["GET", "POST"])
def customer_register_verify():
    customer_id = session.get("pending_register_customer_id")
    if not customer_id:
        return redirect(url_for("customer_register"))

    if request.method == "POST":
        code = request.form["otp"].strip()
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM customers WHERE customer_id=%s", (customer_id,))
                customer = cur.fetchone()
                if not customer:
                    return redirect(url_for("customer_register"))

                if not customer["email_otp_expires"] or customer["email_otp_expires"] < datetime.now():
                    flash("Verification code expired. Register again to request a new code.", "danger")
                    return redirect(url_for("customer_register"))

                if customer["email_otp_hash"] != hash_password(code):
                    flash("Invalid verification code.", "danger")
                    return redirect(url_for("customer_register_verify"))

                cur.execute("""
                    UPDATE customers
                    SET is_verified=1, email_otp_hash=NULL, email_otp_expires=NULL
                    WHERE customer_id=%s
                """, (customer_id,))

        session.pop("pending_register_customer_id", None)
        flash("Email verified. You can now log in.", "success")
        return redirect(url_for("customer_login"))

    return render_template("customer_verify.html", title="Verify Registration")

@app.route("/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM customers WHERE email=%s", (email,))
                customer = cur.fetchone()

                if not customer or customer["password_hash"] != hash_password(password):
                    flash("Invalid email or password.", "danger")
                    return redirect(url_for("customer_login"))

                if not customer["is_verified"]:
                    flash("Your account is not verified yet.", "warning")
                    return redirect(url_for("customer_register"))

                otp, sent = issue_customer_otp(cur, customer["customer_id"], customer["email"], "login")

        session["pending_login_customer_id"] = customer["customer_id"]
        if sent:
            flash("A login verification code was sent to your Gmail/email.", "success")
        else:
            flash(f"SMTP is not configured. Development OTP: {otp}", "warning")
        return redirect(url_for("customer_login_verify"))

    return render_template("customer_login.html")

@app.route("/login/verify", methods=["GET", "POST"])
def customer_login_verify():
    customer_id = session.get("pending_login_customer_id")
    if not customer_id:
        return redirect(url_for("customer_login"))

    if request.method == "POST":
        code = request.form["otp"].strip()
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM customers WHERE customer_id=%s", (customer_id,))
                customer = cur.fetchone()

                if not customer["email_otp_expires"] or customer["email_otp_expires"] < datetime.now():
                    flash("Login code expired. Please log in again.", "danger")
                    return redirect(url_for("customer_login"))

                if customer["email_otp_hash"] != hash_password(code):
                    flash("Invalid verification code.", "danger")
                    return redirect(url_for("customer_login_verify"))

                cur.execute("""
                    UPDATE customers
                    SET email_otp_hash=NULL, email_otp_expires=NULL
                    WHERE customer_id=%s
                """, (customer_id,))

        session.pop("pending_login_customer_id", None)
        session["customer_id"] = customer["customer_id"]
        session["customer_name"] = customer["full_name"]
        session["customer_email"] = customer["email"]
        return redirect(url_for("customer_account"))

    return render_template("customer_verify.html", title="Verify Login")

@app.route("/logout")
def customer_logout():
    for key in ["customer_id", "customer_name", "customer_email"]:
        session.pop(key, None)
    return redirect(url_for("home"))

@app.route("/account")
@customer_login_required
def customer_account():
    customer_id = session["customer_id"]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM customers WHERE customer_id=%s", (customer_id,))
            customer = cur.fetchone()

            cur.execute("""
                SELECT v.*
                FROM vehicle_likes l
                JOIN vehicles v ON l.vehicle_id=v.vehicle_id
                WHERE l.customer_id=%s
                ORDER BY l.created_at DESC
            """, (customer_id,))
            liked = cur.fetchall()

            cur.execute("""
                SELECT r.*, v.make_model, v.image_url, v.location
                FROM rentals r
                JOIN vehicles v ON r.vehicle_id=v.vehicle_id
                WHERE r.customer_id=%s
                ORDER BY r.created_at DESC
            """, (customer_id,))
            rentals = cur.fetchall()

            cur.execute("""
                SELECT i.*, v.make_model
                FROM inquiries i
                JOIN vehicles v ON i.vehicle_id=v.vehicle_id
                WHERE i.customer_id=%s
                ORDER BY i.updated_at DESC
            """, (customer_id,))
            inquiries = cur.fetchall()

    return render_template("customer_account.html", customer=customer, liked=liked, rentals=rentals, inquiries=inquiries)

# ---------------- CUSTOMER ACTIONS ----------------

@app.route("/like/<int:vehicle_id>", methods=["POST"])
@customer_login_required
def like_vehicle(vehicle_id):
    customer_id = session["customer_id"]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT like_id FROM vehicle_likes WHERE customer_id=%s AND vehicle_id=%s",
                (customer_id, vehicle_id)
            )
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM vehicle_likes WHERE like_id=%s", (row["like_id"],))
            else:
                cur.execute(
                    "INSERT INTO vehicle_likes(customer_id,vehicle_id) VALUES(%s,%s)",
                    (customer_id, vehicle_id)
                )
    return redirect(request.referrer or url_for("vehicles"))

@app.route("/inquiry/new/<int:vehicle_id>", methods=["GET", "POST"])
@customer_login_required
def inquiry_new(vehicle_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vehicles WHERE vehicle_id=%s", (vehicle_id,))
            vehicle = cur.fetchone()
            if not vehicle:
                return "Vehicle not found", 404

            cur.execute("SELECT * FROM customers WHERE customer_id=%s", (session["customer_id"],))
            customer = cur.fetchone()

            if request.method == "POST":
                code = "INQ-" + uuid.uuid4().hex[:8].upper()
                cur.execute("""
                    INSERT INTO inquiries(inquiry_code,customer_id,vehicle_id,subject,status)
                    VALUES(%s,%s,%s,%s,'Open')
                    RETURNING inquiry_id
                """, (code, customer["customer_id"], vehicle_id, request.form["subject"]))
                inquiry_id = cur.fetchone()["inquiry_id"]

                cur.execute("""
                    INSERT INTO inquiry_messages(inquiry_id,sender_type,message)
                    VALUES(%s,'Customer',%s)
                """, (inquiry_id, request.form["message"]))

                send_email(
                    customer["email"],
                    f"Redline Rentals inquiry received - {code}",
                    f"We received your inquiry about {vehicle['make_model']}.\n\n"
                    f"Subject: {request.form['subject']}\n"
                    f"Inquiry code: {code}\n\n"
                    "An agent will reply to this email address. You can also see the inquiry status in your Redline account."
                )

                flash("Inquiry sent. Agent replies will be delivered to your Gmail/email.", "success")
                return redirect(url_for("customer_account"))

    return render_template("inquiry_new.html", vehicle=vehicle, customer=customer)

@app.route("/rent/<int:vehicle_id>", methods=["GET", "POST"])
@customer_login_required
def rent(vehicle_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vehicles WHERE vehicle_id=%s", (vehicle_id,))
            vehicle = cur.fetchone()
            if not vehicle:
                return "Vehicle not found", 404

            cur.execute("SELECT * FROM customers WHERE customer_id=%s", (session["customer_id"],))
            customer = cur.fetchone()

            cur.execute("SELECT * FROM rental_locations WHERE is_active=1 ORDER BY location_name")
            rental_locations = cur.fetchall()

            if request.method == "POST":
                try:
                    quote = rental_quote(vehicle, request.form["start_at"], request.form["end_at"])
                except ValueError as exc:
                    flash(str(exc), "danger")
                    return redirect(request.url)

                try:
                    license_path = upload_image(
                        request.files.get("drivers_license_image"),
                        "licenses",
                        f"customer_{customer['customer_id']}"
                    )
                except ValueError as exc:
                    flash(str(exc), "danger")
                    return redirect(request.url)

                if not license_path and not customer.get("drivers_license_image"):
                    flash("Please capture or upload your driver's license card.", "danger")
                    return redirect(request.url)

                if not license_path:
                    license_path = customer["drivers_license_image"]

                permanent_address = " | ".join([
                    request.form["permanent_region"],
                    request.form["permanent_province"],
                    request.form["permanent_city"],
                    request.form["permanent_barangay"],
                    request.form["permanent_street"]
                ])

                staying_address = " | ".join([
                    request.form["staying_region"],
                    request.form["staying_province"],
                    request.form["staying_city"],
                    request.form["staying_barangay"],
                    request.form["staying_street"]
                ])

                cur.execute("""
                    UPDATE customers SET
                    phone=%s,
                    permanent_region=%s,
                    permanent_province=%s,
                    permanent_city=%s,
                    permanent_barangay=%s,
                    permanent_street=%s,
                    staying_region=%s,
                    staying_province=%s,
                    staying_city=%s,
                    staying_barangay=%s,
                    staying_street=%s,
                    drivers_license_number=%s,
                    drivers_license_image=%s
                    WHERE customer_id=%s
                """, (
                    request.form["phone"],
                    request.form["permanent_region"],
                    request.form["permanent_province"],
                    request.form["permanent_city"],
                    request.form["permanent_barangay"],
                    request.form["permanent_street"],
                    request.form["staying_region"],
                    request.form["staying_province"],
                    request.form["staying_city"],
                    request.form["staying_barangay"],
                    request.form["staying_street"],
                    request.form["drivers_license_number"],
                    license_path,
                    customer["customer_id"]
                ))

                ref = "RNT-" + uuid.uuid4().hex[:10].upper()
                cur.execute("""
                    INSERT INTO rentals(
                        rental_reference,customer_id,vehicle_id,start_at,end_at,
                        dropoff_location,permanent_address_snapshot,staying_address_snapshot,
                        drivers_license_number_snapshot,drivers_license_image_snapshot,
                        rental_days,total_amount,reservation_payment,balance_due,status
                    )
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pending Verification')
                """, (
                    ref,
                    customer["customer_id"],
                    vehicle_id,
                    request.form["start_at"],
                    request.form["end_at"],
                    request.form["dropoff_location"],
                    permanent_address,
                    staying_address,
                    request.form["drivers_license_number"],
                    license_path,
                    quote["days"],
                    quote["total"],
                    quote["reservation_payment"],
                    quote["balance"]
                ))

                send_email(
                    customer["email"],
                    f"Redline Rentals request received - {ref}",
                    f"Your rental request for {vehicle['make_model']} has been received.\n\n"
                    f"Reference: {ref}\n"
                    f"Rental total: {money(quote['total'])}\n"
                    f"Required reservation payment: {money(quote['reservation_payment'])}\n"
                    f"Drop-off location: {request.form['dropoff_location']}\n\n"
                    "An agent will verify your request."
                )

                flash(f"Rental request submitted. Fixed reservation payment: {money(quote['reservation_payment'])}.", "success")
                return redirect(url_for("rental_status", reference=ref))

    return render_template(
        "rent.html",
        vehicle=vehicle,
        customer=customer,
        rental_locations=rental_locations
    )

@app.route("/rental/<reference>")
@customer_login_required
def rental_status(reference):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, c.full_name, c.email, c.phone,
                       v.make_model, v.image_url, v.location
                FROM rentals r
                JOIN customers c ON r.customer_id=c.customer_id
                JOIN vehicles v ON r.vehicle_id=v.vehicle_id
                WHERE r.rental_reference=%s AND r.customer_id=%s
            """, (reference, session["customer_id"]))
            rental = cur.fetchone()
            if not rental:
                return "Rental not found", 404

            cur.execute(
                "SELECT * FROM rental_payments WHERE rental_id=%s ORDER BY paid_at",
                (rental["rental_id"],)
            )
            payments = cur.fetchall()

    return render_template("rental_status.html", rental=rental, payments=payments)

@app.route("/return/<reference>", methods=["GET", "POST"])
@customer_login_required
def customer_return_review(reference):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, c.full_name, v.make_model
                FROM rentals r
                JOIN customers c ON r.customer_id=c.customer_id
                JOIN vehicles v ON r.vehicle_id=v.vehicle_id
                WHERE r.rental_reference=%s AND r.customer_id=%s
            """, (reference, session["customer_id"]))
            rental = cur.fetchone()
            if not rental:
                return "Rental not found", 404

            if request.method == "POST":
                cur.execute("""
                    INSERT INTO reviews(rental_id,customer_id,vehicle_id,rating,comment)
                    VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT (rental_id) DO UPDATE
                    SET rating=EXCLUDED.rating, comment=EXCLUDED.comment
                """, (
                    rental["rental_id"],
                    rental["customer_id"],
                    rental["vehicle_id"],
                    request.form["rating"],
                    request.form.get("comment", "")
                ))
                cur.execute("""
                    UPDATE rentals
                    SET return_rating_submitted=1, status='Ready for Return Inspection'
                    WHERE rental_id=%s
                """, (rental["rental_id"],))

                flash("Required return rating submitted.", "success")
                return redirect(url_for("rental_status", reference=reference))

    return render_template("return_review.html", rental=rental)

# ---------------- STAFF AUTH ----------------

@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM staff WHERE email=%s AND is_active=1",
                    (request.form["email"].strip().lower(),)
                )
                staff = cur.fetchone()

        if staff and staff["password_hash"] == hash_password(request.form["password"]):
            session.clear()
            session.update(
                staff_id=staff["staff_id"],
                staff_name=staff["full_name"],
                role=staff["role"],
                staff_profile_image=staff["profile_image"]
            )
            return redirect(url_for("staff_dashboard"))

        flash("Invalid staff account.", "danger")

    return render_template("staff_login.html")

@app.route("/staff/logout")
def staff_logout():
    session.clear()
    return redirect(url_for("staff_login"))

@app.route("/staff")
@require_roles()
def staff_dashboard():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) c FROM rentals WHERE status NOT IN ('Completed','Cancelled')")
            active_rentals = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM vehicles WHERE status='Available'")
            available = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM inquiries WHERE status='Open'")
            open_inquiries = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM inventory_items WHERE quantity<=reorder_level")
            low_stock = cur.fetchone()["c"]
            finance = {p: finance_summary(cur, p) for p in ("day", "week", "month", "year")}

    return render_template(
        "staff_dashboard.html",
        role=session["role"],
        active_rentals=active_rentals,
        available=available,
        open_inquiries=open_inquiries,
        low_stock=low_stock,
        finance=finance,
        STAFF_ROLES=STAFF_ROLES
    )

# ---------------- AGENT ----------------

@app.route("/staff/inquiries")
@require_roles("agent", "manager", "owner")
def staff_inquiries():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.*, v.make_model, c.full_name AS customer_name, c.email, c.phone
                FROM inquiries i
                JOIN vehicles v ON i.vehicle_id=v.vehicle_id
                JOIN customers c ON i.customer_id=c.customer_id
                ORDER BY CASE i.status WHEN 'Open' THEN 1 WHEN 'Waiting Customer' THEN 2 WHEN 'Closed' THEN 3 ELSE 0 END, i.updated_at DESC
            """)
            rows = cur.fetchall()

    return render_template("staff_inquiries.html", inquiries=rows)

@app.route("/staff/inquiry/<int:inquiry_id>", methods=["GET", "POST"])
@require_roles("agent", "manager", "owner")
def staff_inquiry_thread(inquiry_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.*, v.make_model,
                       c.full_name AS customer_name, c.email, c.phone
                FROM inquiries i
                JOIN vehicles v ON i.vehicle_id=v.vehicle_id
                JOIN customers c ON i.customer_id=c.customer_id
                WHERE i.inquiry_id=%s
            """, (inquiry_id,))
            inquiry = cur.fetchone()

            if not inquiry:
                return "Inquiry not found", 404

            if request.method == "POST":
                if request.form.get("message"):
                    reply = request.form["message"].strip()
                    cur.execute("""
                        INSERT INTO inquiry_messages(inquiry_id,sender_type,staff_id,message,email_sent)
                        VALUES(%s,'Agent',%s,%s,0)
                        RETURNING message_id
                    """, (inquiry_id, session["staff_id"], reply))
                    message_id = cur.fetchone()["message_id"]

                    sent = send_email(
                        inquiry["email"],
                        f"Redline Rentals reply - {inquiry['inquiry_code']}",
                        f"Hello {inquiry['customer_name']},\n\n"
                        f"Our agent replied to your inquiry about {inquiry['make_model']}.\n\n"
                        f"Subject: {inquiry['subject']}\n"
                        f"Reply:\n{reply}\n\n"
                        f"Inquiry code: {inquiry['inquiry_code']}\n\n"
                        "You may create another inquiry from your Redline account if you have more questions."
                    )

                    cur.execute(
                        "UPDATE inquiry_messages SET email_sent=%s WHERE message_id=%s",
                        (1 if sent else 0, message_id)
                    )
                    cur.execute("""
                        UPDATE inquiries
                        SET assigned_agent_id=%s, status='Waiting Customer'
                        WHERE inquiry_id=%s
                    """, (session["staff_id"], inquiry_id))

                    flash(
                        "Reply saved and emailed to the customer." if sent
                        else "Reply saved, but SMTP is not configured so email was not sent.",
                        "success" if sent else "warning"
                    )

                if request.form.get("action") == "close":
                    cur.execute("UPDATE inquiries SET status='Closed' WHERE inquiry_id=%s", (inquiry_id,))

                return redirect(url_for("staff_inquiry_thread", inquiry_id=inquiry_id))

            cur.execute(
                "SELECT * FROM inquiry_messages WHERE inquiry_id=%s ORDER BY created_at",
                (inquiry_id,)
            )
            messages = cur.fetchall()

    return render_template("staff_inquiry_thread.html", inquiry=inquiry, messages=messages)

@app.route("/staff/rentals")
@require_roles("agent", "manager", "owner", "dropoff_driver", "guard")
def staff_rentals():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, c.full_name, c.phone, c.email,
                       v.make_model, v.location
                FROM rentals r
                JOIN customers c ON r.customer_id=c.customer_id
                JOIN vehicles v ON r.vehicle_id=v.vehicle_id
                ORDER BY r.created_at DESC
            """)
            rows = cur.fetchall()

    return render_template("staff_rentals.html", rentals=rows)

@app.route("/staff/rental/<int:rental_id>/verify", methods=["POST"])
@require_roles("agent", "manager", "owner")
def verify_rental(rental_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, c.email, c.full_name, v.make_model
                FROM rentals r
                JOIN customers c ON r.customer_id=c.customer_id
                JOIN vehicles v ON r.vehicle_id=v.vehicle_id
                WHERE r.rental_id=%s
            """, (rental_id,))
            rental = cur.fetchone()

            if request.form["action"] == "approve":
                cur.execute("""
                    UPDATE rentals
                    SET status='Reserved', verified_by=%s, verified_at=NOW()
                    WHERE rental_id=%s
                """, (session["staff_id"], rental_id))
                subject = f"Rental approved - {rental['rental_reference']}"
                body = (
                    f"Hello {rental['full_name']},\n\n"
                    f"Your rental request for {rental['make_model']} has been approved.\n"
                    f"Reference: {rental['rental_reference']}\n"
                    f"Required reservation payment: {money(rental['reservation_payment'])}"
                )
            else:
                cur.execute("""
                    UPDATE rentals
                    SET status='Cancelled', verified_by=%s, verified_at=NOW()
                    WHERE rental_id=%s
                """, (session["staff_id"], rental_id))
                subject = f"Rental request update - {rental['rental_reference']}"
                body = (
                    f"Hello {rental['full_name']},\n\n"
                    f"Your rental request for {rental['make_model']} was not approved.\n"
                    f"Reference: {rental['rental_reference']}"
                )

            send_email(rental["email"], subject, body)

    return redirect(url_for("staff_rentals"))

@app.route("/staff/rental/<int:rental_id>/payment", methods=["POST"])
@require_roles("agent", "manager", "owner")
def rental_payment(rental_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, c.email, c.full_name, v.make_model
                FROM rentals r
                JOIN customers c ON r.customer_id=c.customer_id
                JOIN vehicles v ON r.vehicle_id=v.vehicle_id
                WHERE r.rental_id=%s
            """, (rental_id,))
            rental = cur.fetchone()

            cur.execute(
                "SELECT COALESCE(SUM(amount),0) paid FROM rental_payments WHERE rental_id=%s",
                (rental_id,)
            )
            paid = float(cur.fetchone()["paid"])
            payment_type = request.form["payment_type"]

            if payment_type == "Reservation":
                due = max(0, float(rental["reservation_payment"]) - paid)
            else:
                due = max(0, float(rental["total_amount"]) - paid)

            if due <= 0:
                flash("No payment is currently due.", "warning")
                return redirect(url_for("staff_rentals"))

            invoice = "INV-" + uuid.uuid4().hex[:10].upper()

            cur.execute("""
                INSERT INTO rental_payments(
                    rental_id,invoice_number,payment_type,amount,payment_method,received_by
                )
                VALUES(%s,%s,%s,%s,%s,%s)
            """, (
                rental_id,
                invoice,
                payment_type,
                due,
                request.form["payment_method"],
                session["staff_id"]
            ))

            cur.execute("""
                INSERT INTO income_entries(
                    source,reference_id,description,amount,entry_date,created_by
                )
                VALUES('Rental',%s,%s,%s,CURRENT_DATE,%s)
            """, (
                rental_id,
                f"{payment_type} payment - {invoice}",
                due,
                session["staff_id"]
            ))

            cur.execute("""
                UPDATE rentals
                SET balance_due=GREATEST(
                    0,
                    total_amount - (
                        SELECT COALESCE(SUM(amount),0)
                        FROM rental_payments
                        WHERE rental_id=%s
                    )
                )
                WHERE rental_id=%s
            """, (rental_id, rental_id))

            send_email(
                rental["email"],
                f"Redline Rentals payment receipt - {invoice}",
                f"Hello {rental['full_name']},\n\n"
                f"Payment received for {rental['make_model']}.\n"
                f"Reference: {rental['rental_reference']}\n"
                f"Invoice: {invoice}\n"
                f"Payment type: {payment_type}\n"
                f"Amount: {money(due)}\n"
                f"Method: {request.form['payment_method']}"
            )

    flash(f"System payment of {money(due)} recorded.", "success")
    return redirect(url_for("staff_rentals"))

@app.route("/staff/rental/<int:rental_id>/status", methods=["POST"])
@require_roles("agent", "manager", "owner", "dropoff_driver", "guard")
def update_rental_status(rental_id):
    status = request.form["status"]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rentals WHERE rental_id=%s", (rental_id,))
            rental = cur.fetchone()

            if status == "Completed" and not rental["return_rating_submitted"]:
                flash("Customer rating is mandatory before completion.", "danger")
                return redirect(url_for("staff_rentals"))

            cur.execute("UPDATE rentals SET status=%s WHERE rental_id=%s", (status, rental_id))

            if status == "Active":
                cur.execute("UPDATE vehicles SET status='On Rent' WHERE vehicle_id=%s", (rental["vehicle_id"],))
            elif status == "Completed":
                cur.execute("""
                    UPDATE vehicles
                    SET status='Available', rental_count=rental_count+1
                    WHERE vehicle_id=%s
                """, (rental["vehicle_id"],))

    return redirect(url_for("staff_rentals"))

# ---------------- OWNER STAFF MANAGEMENT ----------------

@app.route("/staff/team", methods=["GET", "POST"])
@require_roles("owner")
def manage_staff():
    with db() as conn:
        with conn.cursor() as cur:
            if request.method == "POST":
                try:
                    profile_image = upload_image(
                        request.files.get("profile_image"),
                        "staff",
                        "staff"
                    )
                except ValueError as exc:
                    flash(str(exc), "danger")
                    return redirect(url_for("manage_staff"))

                cur.execute("""
                    INSERT INTO staff(
                        full_name,email,phone,password_hash,role,base_wage,profile_image,is_active
                    )
                    VALUES(%s,%s,%s,%s,%s,%s,%s,1)
                """, (
                    request.form["full_name"],
                    request.form["email"].strip().lower(),
                    request.form["phone"],
                    hash_password(request.form["password"]),
                    request.form["role"],
                    request.form["base_wage"],
                    profile_image
                ))

                send_email(
                    request.form["email"].strip().lower(),
                    "Your Redline Rentals staff account",
                    f"Hello {request.form['full_name']},\n\n"
                    f"Your staff account has been created.\n"
                    f"Role: {STAFF_ROLES[request.form['role']]}\n"
                    f"Login email: {request.form['email'].strip().lower()}\n\n"
                    "Please contact the owner for your temporary password if it was not provided to you directly."
                )

                flash("Staff account created.", "success")
                return redirect(url_for("manage_staff"))

            cur.execute("SELECT * FROM staff ORDER BY is_active DESC, role, full_name")
            staff_rows = cur.fetchall()

    return render_template("manage_staff.html", staff_rows=staff_rows, STAFF_ROLES=STAFF_ROLES)

@app.route("/staff/team/<int:staff_id>/toggle", methods=["POST"])
@require_roles("owner")
def toggle_staff(staff_id):
    if staff_id == session["staff_id"]:
        flash("You cannot disable your own owner account.", "warning")
        return redirect(url_for("manage_staff"))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE staff SET is_active=1 - is_active WHERE staff_id=%s", (staff_id,))

    return redirect(url_for("manage_staff"))

# ---------------- MANAGER / OWNER FLEET ----------------

@app.route("/staff/vehicles", methods=["GET", "POST"])
@require_roles("manager", "owner")
def manage_vehicles():
    with db() as conn:
        with conn.cursor() as cur:
            if request.method == "POST":
                cur.execute("""
                    INSERT INTO vehicles(
                        vehicle_type,category,make_model,plate_number,location,
                        passenger_capacity,price_per_day,status,transmission,
                        fuel_type,image_url,description,is_published
                    )
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                """, (
                    request.form["vehicle_type"],
                    request.form["category"],
                    request.form["make_model"],
                    request.form["plate_number"],
                    request.form["location"],
                    request.form["passenger_capacity"],
                    request.form["price_per_day"],
                    request.form["status"],
                    request.form["transmission"],
                    request.form["fuel_type"],
                    request.form["image_url"],
                    request.form["description"]
                ))
                return redirect(url_for("manage_vehicles"))

            cur.execute("SELECT * FROM vehicles ORDER BY created_at DESC")
            rows = cur.fetchall()

    return render_template("manage_vehicles.html", vehicles=rows)

@app.route("/staff/vehicle/<int:vehicle_id>/update", methods=["POST"])
@require_roles("manager", "owner")
def update_vehicle(vehicle_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE vehicles
                SET price_per_day=%s,status=%s,location=%s,
                    passenger_capacity=%s,is_published=%s
                WHERE vehicle_id=%s
            """, (
                request.form["price_per_day"],
                request.form["status"],
                request.form["location"],
                request.form["passenger_capacity"],
                1 if request.form.get("is_published") else 0,
                vehicle_id
            ))
    return redirect(url_for("manage_vehicles"))

# ---------------- INVENTORY + SERVICE ----------------

@app.route("/staff/inventory", methods=["GET", "POST"])
@require_roles("manager", "owner", "mechanic", "cleaner")
def inventory():
    with db() as conn:
        with conn.cursor() as cur:
            if request.method == "POST" and session["role"] in ("manager", "owner"):
                cur.execute("""
                    INSERT INTO inventory_items(
                        item_name,category,unit,quantity,reorder_level,unit_cost
                    )
                    VALUES(%s,%s,%s,%s,%s,%s)
                """, (
                    request.form["item_name"],
                    request.form["category"],
                    request.form["unit"],
                    request.form["quantity"],
                    request.form["reorder_level"],
                    request.form["unit_cost"]
                ))
                return redirect(url_for("inventory"))

            cur.execute("SELECT * FROM inventory_items ORDER BY category,item_name")
            items = cur.fetchall()

            cur.execute("""
                SELECT u.*, i.item_name, s.full_name, v.make_model
                FROM stock_usage u
                JOIN inventory_items i ON u.item_id=i.item_id
                JOIN staff s ON u.staff_id=s.staff_id
                LEFT JOIN vehicles v ON u.vehicle_id=v.vehicle_id
                ORDER BY u.created_at DESC
                LIMIT 30
            """)
            usage = cur.fetchall()

            cur.execute("SELECT vehicle_id,make_model FROM vehicles ORDER BY make_model")
            vehicles = cur.fetchall()

    return render_template("inventory.html", items=items, usage=usage, vehicles=vehicles)

@app.route("/staff/inventory/use", methods=["POST"])
@require_roles("mechanic", "cleaner", "manager", "owner")
def inventory_use():
    qty = float(request.form["quantity_used"])
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM inventory_items WHERE item_id=%s", (request.form["item_id"],))
            item = cur.fetchone()

            if not item or float(item["quantity"]) < qty:
                flash("Insufficient stock.", "danger")
                return redirect(url_for("inventory"))

            cur.execute("""
                INSERT INTO stock_usage(
                    item_id,staff_id,vehicle_id,quantity_used,purpose,evidence_url
                )
                VALUES(%s,%s,%s,%s,%s,%s)
            """, (
                request.form["item_id"],
                session["staff_id"],
                request.form.get("vehicle_id") or None,
                qty,
                request.form["purpose"],
                request.form.get("evidence_url", "")
            ))

            cur.execute(
                "UPDATE inventory_items SET quantity=quantity-%s WHERE item_id=%s",
                (qty, request.form["item_id"])
            )

            expense = round(qty * float(item["unit_cost"]), 2)
            category = "Cleaning" if item["category"] == "Cleaning" else "Maintenance"

            cur.execute("""
                INSERT INTO expense_entries(
                    category,description,amount,expense_date,created_by
                )
                VALUES(%s,%s,%s,CURRENT_DATE,%s)
            """, (
                category,
                f"Stock usage: {item['item_name']} - {request.form['purpose']}",
                expense,
                session["staff_id"]
            ))

    flash("Usage recorded; stock and expense updated.", "success")
    return redirect(url_for("inventory"))

@app.route("/staff/maintenance", methods=["GET", "POST"])
@require_roles("mechanic", "cleaner", "manager", "owner")
def maintenance_logs():
    with db() as conn:
        with conn.cursor() as cur:
            if request.method == "POST":
                service_type = request.form["service_type"]
                selected_task = request.form["details"]

                if selected_task not in SERVICE_TASKS.get(service_type, []):
                    flash("Please choose a valid fixed service task.", "danger")
                    return redirect(url_for("maintenance_logs"))

                notes = request.form.get("notes", "").strip()
                details = selected_task if not notes else f"{selected_task} - {notes}"

                cur.execute("""
                    INSERT INTO service_logs(
                        vehicle_id,staff_id,service_type,details,cost,evidence_url
                    )
                    VALUES(%s,%s,%s,%s,%s,%s)
                """, (
                    request.form["vehicle_id"],
                    session["staff_id"],
                    service_type,
                    details,
                    request.form["cost"],
                    request.form.get("evidence_url", "")
                ))

                if float(request.form["cost"]) > 0:
                    category = "Cleaning" if service_type == "Cleaning" else "Maintenance"
                    cur.execute("""
                        INSERT INTO expense_entries(
                            category,description,amount,expense_date,created_by
                        )
                        VALUES(%s,%s,%s,CURRENT_DATE,%s)
                    """, (
                        category,
                        details,
                        request.form["cost"],
                        session["staff_id"]
                    ))

                return redirect(url_for("maintenance_logs"))

            cur.execute("""
                SELECT l.*, v.make_model, s.full_name
                FROM service_logs l
                JOIN vehicles v ON l.vehicle_id=v.vehicle_id
                JOIN staff s ON l.staff_id=s.staff_id
                ORDER BY l.created_at DESC
            """)
            logs = cur.fetchall()

            cur.execute("SELECT vehicle_id,make_model FROM vehicles ORDER BY make_model")
            vehicles = cur.fetchall()

    return render_template(
        "maintenance.html",
        logs=logs,
        vehicles=vehicles,
        SERVICE_TASKS=SERVICE_TASKS
    )

# ---------------- FINANCE ----------------

@app.route("/staff/finance", methods=["GET", "POST"])
@require_roles("manager", "owner")
def finance():
    with db() as conn:
        with conn.cursor() as cur:
            if request.method == "POST":
                if request.form["kind"] == "expense":
                    cur.execute("""
                        INSERT INTO expense_entries(
                            category,description,amount,expense_date,created_by
                        )
                        VALUES(%s,%s,%s,%s,%s)
                    """, (
                        request.form["category"],
                        request.form["description"],
                        request.form["amount"],
                        request.form["entry_date"],
                        session["staff_id"]
                    ))
                else:
                    cur.execute("""
                        INSERT INTO income_entries(
                            source,description,amount,entry_date,created_by
                        )
                        VALUES(%s,%s,%s,%s,%s)
                    """, (
                        request.form["source"],
                        request.form["description"],
                        request.form["amount"],
                        request.form["entry_date"],
                        session["staff_id"]
                    ))
                return redirect(url_for("finance"))

            summaries = {p: finance_summary(cur, p) for p in ("day", "week", "month", "year")}

            cur.execute("SELECT * FROM income_entries ORDER BY entry_date DESC,created_at DESC LIMIT 30")
            incomes = cur.fetchall()

            cur.execute("SELECT * FROM expense_entries ORDER BY expense_date DESC,created_at DESC LIMIT 30")
            expenses = cur.fetchall()

    return render_template("finance.html", summaries=summaries, incomes=incomes, expenses=expenses)

@app.route("/staff/reports")
@require_roles("manager", "owner")
def reports():
    period = request.args.get("period", "month")

    with db() as conn:
        with conn.cursor() as cur:
            summary = finance_summary(cur, period)

            cur.execute("""
                SELECT v.vehicle_type,v.make_model,v.rental_count,v.status,
                       COALESCE(SUM(r.total_amount),0) booked_value
                FROM vehicles v
                LEFT JOIN rentals r ON v.vehicle_id=r.vehicle_id
                GROUP BY v.vehicle_id
                ORDER BY v.rental_count DESC
            """)
            fleet = cur.fetchall()

            cur.execute("""
                SELECT category,SUM(amount) total
                FROM expense_entries
                WHERE expense_date BETWEEN %s AND %s
                GROUP BY category
                ORDER BY total DESC
            """, (summary["start"], summary["end"]))
            expense_breakdown = cur.fetchall()

    return render_template(
        "reports.html",
        summary=summary,
        fleet=fleet,
        expense_breakdown=expense_breakdown,
        period=period
    )

# ---------------- PAYROLL ----------------

@app.route("/staff/payroll", methods=["GET", "POST"])
@require_roles("owner")
def payroll():
    with db() as conn:
        with conn.cursor() as cur:
            if request.method == "POST":
                cur.execute("SELECT * FROM staff WHERE staff_id=%s", (request.form["staff_id"],))
                staff_row = cur.fetchone()

                amount = float(request.form.get("amount") or staff_row["base_wage"])

                cur.execute("""
                    INSERT INTO payroll(
                        staff_id,period_start,period_end,amount,status,processed_by
                    )
                    VALUES(%s,%s,%s,%s,'Pending',%s)
                """, (
                    request.form["staff_id"],
                    request.form["period_start"],
                    request.form["period_end"],
                    amount,
                    session["staff_id"]
                ))

                return redirect(url_for("payroll"))

            cur.execute("""
                SELECT staff_id,full_name,email,role,base_wage,profile_image
                FROM staff
                WHERE role<>'owner' AND is_active=1
                ORDER BY role,full_name
            """)
            staff_rows = cur.fetchall()

            cur.execute("""
                SELECT p.*, s.full_name, s.email, s.role, s.profile_image
                FROM payroll p
                JOIN staff s ON p.staff_id=s.staff_id
                ORDER BY p.created_at DESC
            """)
            payroll_rows = cur.fetchall()

    return render_template(
        "payroll.html",
        staff=staff_rows,
        payroll_rows=payroll_rows,
        STAFF_ROLES=STAFF_ROLES
    )

@app.route("/staff/payroll/<int:payroll_id>/pay", methods=["POST"])
@require_roles("owner")
def pay_wage(payroll_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, s.full_name, s.email, s.role
                FROM payroll p
                JOIN staff s ON p.staff_id=s.staff_id
                WHERE p.payroll_id=%s
            """, (payroll_id,))
            payroll_row = cur.fetchone()

            if not payroll_row:
                return "Payroll record not found", 404

            if payroll_row["status"] == "Paid":
                flash("This payroll record is already paid.", "warning")
                return redirect(url_for("payroll"))

            receipt_number = "PAY-" + uuid.uuid4().hex[:10].upper()

            sent = send_email(
                payroll_row["email"],
                f"Redline Rentals payroll receipt - {receipt_number}",
                f"Hello {payroll_row['full_name']},\n\n"
                "Your wage payment has been recorded by the owner.\n\n"
                f"Receipt: {receipt_number}\n"
                f"Role: {STAFF_ROLES[payroll_row['role']]}\n"
                f"Payroll period: {payroll_row['period_start']} to {payroll_row['period_end']}\n"
                f"Amount: {money(payroll_row['amount'])}\n"
                f"Paid date: {date.today().isoformat()}\n\n"
                "This email serves as your online payroll receipt."
            )

            cur.execute("""
                UPDATE payroll
                SET status='Paid',
                    paid_date=CURRENT_DATE,
                    receipt_number=%s,
                    receipt_email_sent=%s
                WHERE payroll_id=%s
            """, (receipt_number, 1 if sent else 0, payroll_id))

    flash(
        "Payroll marked paid and receipt emailed to the staff member."
        if sent else
        "Payroll marked paid, but SMTP is not configured so the email receipt was not sent.",
        "success" if sent else "warning"
    )
    return redirect(url_for("payroll"))

@app.route("/api/finance/<period>")
@require_roles("manager", "owner")
def api_finance(period):
    with db() as conn:
        with conn.cursor() as cur:
            summary = finance_summary(cur, period)
    summary["start"] = summary["start"].isoformat()
    summary["end"] = summary["end"].isoformat()
    return jsonify(summary)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
