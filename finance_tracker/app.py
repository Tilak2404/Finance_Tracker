import os

import matplotlib
from flask import Flask, redirect, render_template, request, session
from flask_sqlalchemy import SQLAlchemy

matplotlib.use("Agg")

import matplotlib.pyplot as plt

app = Flask(__name__)
app.secret_key = "your_secret_key"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(100))
    description = db.Column(db.String(255))


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
        plt.pie(
            values,
            labels=labels,
            autopct="%1.1f%%",
            startangle=90,
        )

    centre_circle = plt.Circle((0, 0), 0.70, fc="white")
    fig = plt.gcf()
    fig.gca().add_artist(centre_circle)
    plt.text(
        0,
        0,
        f"₹{balance:.2f}",
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

        user = User(username=username, password=password)
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
        user = User.query.filter_by(username=username, password=password).first()

        if user:
            session["user_id"] = user.id
            return redirect("/")

        error = "Invalid username or password."
        return render_template("login.html", error=error)

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")

    transactions = Transaction.query.all()
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
    generate_donut_chart(expense_transactions, balance)

    return render_template(
        "index.html",
        transactions=transactions,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
    )


@app.route("/add", methods=["GET", "POST"])
def add_transaction():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        transaction = Transaction(
            amount=float(request.form["amount"]),
            type=request.form["type"],
            category=request.form.get("category", ""),
            description=request.form.get("description", ""),
        )
        db.session.add(transaction)
        db.session.commit()
        return redirect("/")

    return render_template("add.html")


@app.route("/delete/<int:id>")
def delete_transaction(id):
    if "user_id" not in session:
        return redirect("/login")

    transaction = Transaction.query.get(id)
    if transaction:
        db.session.delete(transaction)
        db.session.commit()
    return redirect("/")


@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_transaction(id):
    if "user_id" not in session:
        return redirect("/login")

    transaction = Transaction.query.get(id)
    if not transaction:
        return redirect("/")

    if request.method == "POST":
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

    transactions = Transaction.query.all()
    expense_transactions = Transaction.query.filter_by(type="expense").all()
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
