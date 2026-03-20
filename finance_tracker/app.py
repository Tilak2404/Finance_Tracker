import os
from datetime import datetime

import matplotlib
from flask import Flask, redirect, render_template, request, session
from flask_sqlalchemy import SQLAlchemy
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
    budget = Budget.query.filter_by(user_id=session["user_id"]).first()
    remaining_budget = budget.amount - total_expense if budget else None
    budget_exceeded = budget is not None and total_expense > budget.amount
    generate_donut_chart(expense_transactions, balance)

    return render_template(
        "index.html",
        transactions=transactions,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        filter_date=filter_date,
        filter_month=filter_month,
        budget=budget,
        remaining_budget=remaining_budget,
        budget_exceeded=budget_exceeded,
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

    return render_template("chart.html")


if __name__ == "__main__":
    app.run(debug=True)
