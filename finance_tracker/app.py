import os
from datetime import datetime, timedelta

import matplotlib
from flask import Flask, redirect, render_template, request, send_file, session
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

matplotlib.use("Agg")

import matplotlib.pyplot as plt

app = Flask(__name__)
app.secret_key = "your_secret_key"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)


class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, unique=True)
    amount = db.Column(db.Float, nullable=False)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(100))
    description = db.Column(db.String(255))
    user_id = db.Column(db.Integer)
    date = db.Column(db.DateTime, default=datetime.utcnow)


def generate_donut_chart(expense_transactions, balance):
    static_folder = os.path.join(app.root_path, "static")
    os.makedirs(static_folder, exist_ok=True)
    chart_path = os.path.join(static_folder, "chart.png")

    categories = ["Food", "Transport", "Bills", "Shopping", "Entertainment", "Health"]
    category_totals = {
        "Food": 0,
        "Transport": 0,
        "Bills": 0,
        "Shopping": 0,
        "Entertainment": 0,
        "Health": 0,
        "Miscellaneous": 0,
    }

    for transaction in expense_transactions:
        normalized_category = (transaction.category or "").strip().title()
        if normalized_category in categories:
            category_totals[normalized_category] += transaction.amount
        else:
            category_totals["Miscellaneous"] += transaction.amount

    labels = []
    values = []
    for category, total in category_totals.items():
        if total > 0:
            labels.append(category)
            values.append(total)

    plt.figure(figsize=(6, 6))
    if not values:
        plt.pie([1], colors=["#dfe6e9"], startangle=90)
    else:
        plt.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)

    centre_circle = plt.Circle((0, 0), 0.70, fc="white")
    fig = plt.gcf()
    fig.gca().add_artist(centre_circle)
    plt.text(
        0,
        0,
        f"\u20b9{balance:.2f}",
        horizontalalignment="center",
        verticalalignment="center",
        fontsize=16,
        fontweight="bold",
    )
    plt.text(
        0,
        -0.2,
        "Balance",
        horizontalalignment="center",
        fontsize=10,
    )
    plt.axis("equal")
    plt.title("Expenses by Category")
    plt.savefig(chart_path, bbox_inches="tight")
    plt.close()


def generate_trend_chart(expense_transactions):
    static_folder = os.path.join(app.root_path, "static")
    os.makedirs(static_folder, exist_ok=True)
    trend_path = os.path.join(static_folder, "trend.png")

    expense_by_date = {}
    for transaction in expense_transactions:
        if transaction.date:
            date_key = transaction.date.strftime("%Y-%m-%d")
        else:
            date_key = "Unknown"
        expense_by_date[date_key] = expense_by_date.get(date_key, 0) + transaction.amount

    dates = sorted(expense_by_date.keys())
    values = [expense_by_date[date] for date in dates]

    plt.figure(figsize=(8, 4))
    if not values:
        plt.text(0.5, 0.5, "No Expense Data", ha="center", va="center", fontsize=12)
        plt.axis("off")
    else:
        plt.plot(dates, values, marker="o", color="#2ecc71", linewidth=2)
        plt.fill_between(dates, values, color="#2ecc71", alpha=0.12)
        plt.xlabel("Date")
        plt.ylabel("Expenses")
        plt.title("Expense Trend Over Time")
        plt.xticks(rotation=45, ha="right")
        plt.grid(axis="y", linestyle="--", alpha=0.4)
        plt.tight_layout()

    plt.savefig(trend_path, bbox_inches="tight")
    plt.close()


def build_insights(filtered_expense_transactions, all_expense_transactions):
    insights = []

    if filtered_expense_transactions:
        category_totals = {}
        for transaction in filtered_expense_transactions:
            category_name = (transaction.category or "").strip().title() or "Miscellaneous"
            category_totals[category_name] = category_totals.get(category_name, 0) + transaction.amount

        top_category = max(category_totals, key=category_totals.get)
        insights.append(f"You spent most on {top_category}.")
    else:
        insights.append("Add some expense transactions to unlock spending insights.")

    today = datetime.utcnow().date()
    start_of_this_week = today - timedelta(days=today.weekday())
    start_of_next_week = start_of_this_week + timedelta(days=7)
    start_of_last_week = start_of_this_week - timedelta(days=7)

    this_week_total = sum(
        transaction.amount
        for transaction in all_expense_transactions
        if transaction.date and start_of_this_week <= transaction.date.date() < start_of_next_week
    )
    last_week_total = sum(
        transaction.amount
        for transaction in all_expense_transactions
        if transaction.date and start_of_last_week <= transaction.date.date() < start_of_this_week
    )

    if this_week_total > 0 and last_week_total > 0:
        if this_week_total > last_week_total:
            insights.append(
                f"Your spending is up by \u20b9{this_week_total - last_week_total:.2f} compared with last week."
            )
        elif this_week_total < last_week_total:
            insights.append(
                f"Your spending is down by \u20b9{last_week_total - this_week_total:.2f} compared with last week."
            )
        else:
            insights.append("Your spending matches last week exactly.")
    elif this_week_total > 0 and last_week_total == 0:
        insights.append(f"You have spent \u20b9{this_week_total:.2f} so far this week.")

    return insights


def generate_pdf_report(transactions, total_income, total_expense, balance):
    report_path = os.path.join(app.root_path, "report.pdf")
    document = SimpleDocTemplate(report_path, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph("Finance Report", styles["Title"]))
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(f"Total Income: Rs {total_income:.2f}", styles["Normal"]))
    elements.append(Paragraph(f"Total Expense: Rs {total_expense:.2f}", styles["Normal"]))
    elements.append(Paragraph(f"Balance: Rs {balance:.2f}", styles["Normal"]))
    elements.append(Spacer(1, 16))

    table_data = [["Amount", "Category", "Type"]]
    for transaction in transactions:
        table_data.append(
            [
                f"Rs {transaction.amount:.2f}",
                transaction.category or "Miscellaneous",
                transaction.type.title(),
            ]
        )

    if len(table_data) == 1:
        elements.append(Paragraph("No transactions available.", styles["Normal"]))
    else:
        table = Table(table_data, colWidths=[120, 180, 120])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                    ("BACKGROUND", (0, 1), (-1, -1), colors.whitesmoke),
                ]
            )
        )
        elements.append(table)

    document.build(elements)
    return report_path


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect("/")

    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            error = "Username already exists."
            return render_template("register.html", error=error)

        hashed_password = generate_password_hash(password)
        user = User(username=username, password=hashed_password)
        db.session.add(user)
        db.session.commit()
        return redirect("/login")

    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect("/")

    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            return redirect("/")

        error = "Invalid username or password."
        return render_template("login.html", error=error)

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/set_budget", methods=["POST"])
def set_budget():
    if "user_id" not in session:
        return redirect("/login")

    budget_amount = request.form.get("budget_amount", "").strip()
    try:
        amount = float(budget_amount)
    except ValueError:
        amount = 0.0

    budget = Budget.query.filter_by(user_id=session["user_id"]).first()
    if budget:
        budget.amount = amount
    else:
        budget = Budget(user_id=session["user_id"], amount=amount)
        db.session.add(budget)

    db.session.commit()

    filter_date = request.form.get("filter_date", "")
    filter_month = request.form.get("filter_month", "")
    if filter_date:
        return redirect(f"/?filter_date={filter_date}")
    if filter_month:
        return redirect(f"/?filter_month={filter_month}")
    return redirect("/")


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")

    all_expense_transactions = Transaction.query.filter_by(
        user_id=session["user_id"], type="expense"
    ).all()
    filter_date = request.args.get("filter_date", "")
    filter_month = request.args.get("filter_month", "")

    query = Transaction.query.filter_by(user_id=session["user_id"])

    if filter_date:
        try:
            selected_date = datetime.strptime(filter_date, "%Y-%m-%d").date()
            query = query.filter(func.date(Transaction.date) == selected_date.isoformat())
        except ValueError:
            filter_date = ""
    elif filter_month:
        try:
            month_start = datetime.strptime(filter_month, "%Y-%m")
            if month_start.month == 12:
                month_end = datetime(month_start.year + 1, 1, 1)
            else:
                month_end = datetime(month_start.year, month_start.month + 1, 1)
            query = query.filter(Transaction.date >= month_start, Transaction.date < month_end)
        except ValueError:
            filter_month = ""

    transactions = query.order_by(Transaction.date.desc()).all()
    expense_transactions = [
        transaction for transaction in transactions if transaction.type == "expense"
    ]
    total_income = sum(
        transaction.amount for transaction in transactions if transaction.type == "income"
    )
    total_expense = sum(
        transaction.amount for transaction in transactions if transaction.type == "expense"
    )
    balance = total_income - total_expense
    savings = total_income - total_expense
    if total_income > 0:
        savings_rate = round((savings / total_income) * 100, 2)
    else:
        savings_rate = 0
    budget = Budget.query.filter_by(user_id=session["user_id"]).first()
    remaining_budget = budget.amount - total_expense if budget else None
    budget_exceeded = budget is not None and total_expense > budget.amount
    insights = build_insights(expense_transactions, all_expense_transactions)
    generate_donut_chart(expense_transactions, balance)
    generate_trend_chart(expense_transactions)

    return render_template(
        "index.html",
        transactions=transactions,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        savings_rate=savings_rate,
        filter_date=filter_date,
        filter_month=filter_month,
        budget=budget,
        remaining_budget=remaining_budget,
        budget_exceeded=budget_exceeded,
        insights=insights,
    )


@app.route("/add", methods=["GET", "POST"])
def add_transaction():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        transaction_date = request.form.get("date", "")
        try:
            parsed_date = (
                datetime.strptime(transaction_date, "%Y-%m-%d")
                if transaction_date
                else datetime.utcnow()
            )
        except ValueError:
            parsed_date = datetime.utcnow()

        transaction = Transaction(
            amount=float(request.form["amount"]),
            type=request.form["type"],
            category=request.form.get("category", ""),
            description=request.form.get("description", ""),
            user_id=session["user_id"],
            date=parsed_date,
        )
        db.session.add(transaction)
        db.session.commit()
        return redirect("/")

    return render_template("add.html")


@app.route("/delete/<int:id>")
def delete_transaction(id):
    if "user_id" not in session:
        return redirect("/login")

    transaction = Transaction.query.filter_by(id=id, user_id=session["user_id"]).first()
    if transaction:
        db.session.delete(transaction)
        db.session.commit()
    return redirect("/")


@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_transaction(id):
    if "user_id" not in session:
        return redirect("/login")

    transaction = Transaction.query.filter_by(id=id, user_id=session["user_id"]).first()
    if not transaction:
        return redirect("/")

    if request.method == "POST":
        transaction_date = request.form.get("date", "")
        try:
            if transaction_date:
                transaction.date = datetime.strptime(transaction_date, "%Y-%m-%d")
        except ValueError:
            pass

        transaction.amount = float(request.form["amount"])
        transaction.type = request.form["type"]
        transaction.category = request.form.get("category", "")
        transaction.description = request.form.get("description", "")
        db.session.commit()
        return redirect("/")

    return render_template("edit.html", transaction=transaction)


@app.route("/chart")
def chart():
    if "user_id" not in session:
        return redirect("/login")

    transactions = Transaction.query.filter_by(user_id=session["user_id"]).all()
    expense_transactions = Transaction.query.filter_by(
        user_id=session["user_id"], type="expense"
    ).all()
    total_income = sum(
        transaction.amount for transaction in transactions if transaction.type == "income"
    )
    total_expense = sum(
        transaction.amount for transaction in transactions if transaction.type == "expense"
    )
    balance = total_income - total_expense
    generate_donut_chart(expense_transactions, balance)
    generate_trend_chart(expense_transactions)

    return render_template("chart.html")


@app.route("/download_report")
def download_report():
    if "user_id" not in session:
        return redirect("/login")

    transactions = (
        Transaction.query.filter_by(user_id=session["user_id"])
        .order_by(Transaction.date.desc())
        .all()
    )
    total_income = sum(
        transaction.amount for transaction in transactions if transaction.type == "income"
    )
    total_expense = sum(
        transaction.amount for transaction in transactions if transaction.type == "expense"
    )
    balance = total_income - total_expense

    report_path = generate_pdf_report(transactions, total_income, total_expense, balance)
    return send_file(report_path, as_attachment=True, download_name="report.pdf")


if __name__ == "__main__":
    app.run(debug=True)
