import ast
import calendar
import csv
import math
import os
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from io import StringIO
from urllib.parse import urlencode

import matplotlib
from flask import Flask, flash, jsonify, make_response, redirect, render_template, request, send_file, session
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, inspect, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

matplotlib.use("Agg")

import matplotlib.pyplot as plt

app = Flask(__name__)
import secrets
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
DEFAULT_DATABASE_PATH = os.path.join(app.root_path, "finance.db")
DEFAULT_FAVICON_PATH = os.path.join(app.static_folder, "favicon.svg")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DEFAULT_DATABASE_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
INITIALIZED_DATABASE_URI = None

VALID_TRANSACTION_TYPES = {"income", "expense"}
VALID_TRANSACTION_FILTERS = {"all", "income", "expense"}
KNOWN_PASSWORD_HASH_PREFIXES = ("scrypt:", "pbkdf2:", "argon2:")
TRANSACTION_CATEGORIES = [
    "Food",
    "Transport",
    "Bills",
    "Shopping",
    "Entertainment",
    "Health",
    "Education",
    "Other",
]
CATEGORY_CHART_COLORS = {
    "Food": "#f97316",
    "Transport": "#38bdf8",
    "Bills": "#ef4444",
    "Shopping": "#a855f7",
    "Entertainment": "#f59e0b",
    "Health": "#22c55e",
    "Education": "#818cf8",
    "Other": "#94a3b8",
    "Miscellaneous": "#94a3b8",
}
DASHBOARD_SORT_OPTIONS = {
    "newest": "Newest first",
    "oldest": "Oldest first",
    "highest": "Highest amount",
    "lowest": "Lowest amount",
}
DEFAULT_DASHBOARD_SORT = "newest"
ALLOWED_CALCULATOR_BINARY_OPERATORS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.Mod: lambda left, right: left % right,
}
ALLOWED_CALCULATOR_UNARY_OPERATORS = {
    ast.UAdd: lambda value: value,
    ast.USub: lambda value: -value,
}


def utc_now():
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_username(value):
    return (value or "").strip().lower()


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
    tags = db.Column(db.String)
    user_id = db.Column(db.Integer)
    date = db.Column(db.DateTime, default=utc_now)


def ensure_database_ready():
    global INITIALIZED_DATABASE_URI

    current_database_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if INITIALIZED_DATABASE_URI == current_database_uri:
        return

    if current_database_uri == f"sqlite:///{DEFAULT_DATABASE_PATH}" and not os.path.exists(
        DEFAULT_DATABASE_PATH
    ):
        legacy_database_path = os.path.join(app.instance_path, "database.db")
        if os.path.exists(legacy_database_path):
            shutil.copy2(legacy_database_path, DEFAULT_DATABASE_PATH)

    db.create_all()

    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    schema_changed = False
    column_updates = {
        "transaction": {
            "category": 'ALTER TABLE "transaction" ADD COLUMN category VARCHAR(100)',
            "description": 'ALTER TABLE "transaction" ADD COLUMN description VARCHAR(255)',
            "tags": 'ALTER TABLE "transaction" ADD COLUMN tags VARCHAR',
            "user_id": 'ALTER TABLE "transaction" ADD COLUMN user_id INTEGER',
            "date": 'ALTER TABLE "transaction" ADD COLUMN date DATETIME',
        },
        "budget": {
            "user_id": 'ALTER TABLE budget ADD COLUMN user_id INTEGER',
            "amount": "ALTER TABLE budget ADD COLUMN amount FLOAT",
        },
    }

    for table_name, statements in column_updates.items():
        if table_name not in table_names:
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, statement in statements.items():
            if column_name not in existing_columns:
                db.session.execute(text(statement))
                schema_changed = True

    if schema_changed:
        db.session.commit()

    INITIALIZED_DATABASE_URI = current_database_uri


def verify_user_password(user, entered_password):
    if not user.password.startswith(KNOWN_PASSWORD_HASH_PREFIXES):
        if user.password != entered_password:
            return False, "Incorrect password. If this is an old account, register again with a new password."

        try:
            user.password = generate_password_hash(entered_password)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            return False, "Your account uses an old password format and could not be upgraded."

        return True, "Your account password was upgraded to the secure format."

    try:
        return check_password_hash(user.password, entered_password), None
    except (ValueError, TypeError):
        return False, "Incorrect password. If this is an old account, register again with a new password."


@app.before_request
def initialize_database():
    ensure_database_ready()


@app.route("/favicon.ico")
def favicon():
    if os.path.exists(DEFAULT_FAVICON_PATH):
        return app.send_static_file("favicon.svg")

    return "", 204


def parse_transaction_form(form, default_date=None):
    amount_text = form.get("amount", "").strip()
    transaction_type = form["type"].strip()
    if transaction_type == "income":
        category = "Income"
    else:
        try:
            category = form["category"].strip()
        except KeyError:
            category = ""
    description = form.get("description", "").strip()
    tags = form.get("tags", "").strip()
    date_text = form.get("date", "").strip()

    if not amount_text or not transaction_type:
        return None, "Invalid input. Amount and type are required."

    if transaction_type == "expense" and not category:
        return None, "Invalid input. Category is required."

    try:
        amount = float(amount_text)
    except ValueError:
        return None, "Invalid input. Amount must be a number."

    if amount <= 0:
        return None, "Invalid input. Amount must be greater than 0."

    if transaction_type not in VALID_TRANSACTION_TYPES:
        return None, "Invalid input. Type must be income or expense."

    parsed_date = default_date or datetime.now()
    if date_text:
        try:
            parsed_date = datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            return None, "Invalid input. Date must be valid."

    return {
        "amount": amount,
        "type": transaction_type,
        "category": category,
        "description": description,
        "tags": tags,
        "date": parsed_date,
    }, None


def evaluate_calculation_expression(expression):
    normalized_expression = expression.strip()
    if not normalized_expression:
        raise ValueError("Enter a calculation first.")

    try:
        parsed_expression = ast.parse(normalized_expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("Invalid calculation.") from exc

    def evaluate_node(node):
        if isinstance(node, ast.Expression):
            return evaluate_node(node.body)

        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if isinstance(node.value, bool):
                raise ValueError("Invalid calculation.")
            return float(node.value)

        if isinstance(node, ast.BinOp):
            operation = ALLOWED_CALCULATOR_BINARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ValueError("Only +, -, *, /, and % are allowed.")

            left_value = evaluate_node(node.left)
            right_value = evaluate_node(node.right)
            try:
                result = operation(left_value, right_value)
            except ZeroDivisionError as exc:
                raise ValueError("Division by zero is not allowed.") from exc

            if not math.isfinite(result):
                raise ValueError("Result must be a finite number.")
            return result

        if isinstance(node, ast.UnaryOp):
            operation = ALLOWED_CALCULATOR_UNARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ValueError("Invalid calculation.")

            result = operation(evaluate_node(node.operand))
            if not math.isfinite(result):
                raise ValueError("Result must be a finite number.")
            return result

        raise ValueError("Invalid calculation.")

    result = round(evaluate_node(parsed_expression), 2)
    if result <= 0:
        raise ValueError("Amount must be greater than 0.")

    return result


def generate_donut_chart(expense_transactions, balance):
    static_folder = os.path.join(app.root_path, "static")
    os.makedirs(static_folder, exist_ok=True)
    chart_path = os.path.join(static_folder, "chart.png")

    categories = list(TRANSACTION_CATEGORIES)
    category_totals = {category: 0 for category in categories}
    category_totals["Miscellaneous"] = 0

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

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(6, 6))
    fig.patch.set_facecolor("#020617")
    ax.set_facecolor("#020617")
    pie_colors = [CATEGORY_CHART_COLORS.get(label, "#94a3b8") for label in labels] or ["#22c55e"]

    if not values:
        ax.pie(
            [1],
            colors=pie_colors,
            startangle=90,
            textprops={"color": "white"},
            wedgeprops={"linewidth": 1, "edgecolor": "#0f172a"},
        )
    else:
        ax.pie(
            values,
            labels=labels,
            colors=pie_colors,
            autopct="%1.1f%%",
            startangle=90,
            textprops={"color": "white"},
            wedgeprops={"linewidth": 1, "edgecolor": "#0f172a"},
        )

    centre_circle = plt.Circle((0, 0), 0.70, fc="#020617")
    ax.add_artist(centre_circle)
    plt.text(
        0,
        0,
        f"\u20b9{balance:.2f}",
        color="white",
        horizontalalignment="center",
        verticalalignment="center",
        fontsize=16,
        fontweight="bold",
    )
    plt.text(
        0,
        -0.2,
        "Balance",
        color="white",
        horizontalalignment="center",
        fontsize=10,
    )
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors="white")
    ax.axis("equal")
    plt.title("Expenses by Category", color="white")
    plt.savefig(chart_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


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

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(8, 4))
    fig.patch.set_facecolor("#020617")
    ax.set_facecolor("#020617")

    if not values:
        ax.text(0.5, 0.5, "No Expense Data", ha="center", va="center", fontsize=12, color="white")
        ax.axis("off")
    else:
        ax.plot(dates, values, marker="o", color="#22c55e", linewidth=2)
        ax.fill_between(dates, values, color="#16a34a", alpha=0.18)
        ax.set_xlabel("Date", color="white")
        ax.set_ylabel("Amount", color="white")
        ax.set_title("Expense Trend Over Time", color="white")
        ax.tick_params(colors="white")
        plt.xticks(rotation=45, ha="right")
        plt.grid(color="#1e293b", linestyle="--", linewidth=0.5)

    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    plt.savefig(trend_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


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

    today = utc_now().date()
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


def calculate_future_projection(transactions):
    monthly_totals = {}

    for transaction in transactions:
        transaction_date = transaction.date or utc_now()
        month_key = transaction_date.strftime("%Y-%m")
        monthly_bucket = monthly_totals.setdefault(month_key, {"income": 0.0, "expense": 0.0})

        if transaction.type == "income":
            monthly_bucket["income"] += transaction.amount
        elif transaction.type == "expense":
            monthly_bucket["expense"] += transaction.amount

    month_count = len(monthly_totals)
    if month_count:
        average_monthly_income = sum(
            bucket["income"] for bucket in monthly_totals.values()
        ) / month_count
        average_monthly_expense = sum(
            bucket["expense"] for bucket in monthly_totals.values()
        ) / month_count
    else:
        average_monthly_income = 0.0
        average_monthly_expense = 0.0

    monthly_savings = average_monthly_income - average_monthly_expense

    return {
        "average_monthly_income": round(average_monthly_income, 2),
        "average_monthly_expense": round(average_monthly_expense, 2),
        "monthly_savings": round(monthly_savings, 2),
        "six_month_projection": round(monthly_savings * 6, 2),
        "yearly_savings": round(monthly_savings * 12, 2),
        "months_tracked": month_count,
    }


def normalize_dashboard_filters(args):
    filters = {
        "filter_date": args.get("filter_date", "").strip(),
        "filter_month": args.get("filter_month", "").strip(),
        "search": args.get("search", "").strip(),
        "transaction_type": args.get("transaction_type", "all").strip().lower() or "all",
        "category": args.get("category", "").strip(),
        "sort": args.get("sort", DEFAULT_DASHBOARD_SORT).strip().lower() or DEFAULT_DASHBOARD_SORT,
    }
    messages = []

    if filters["transaction_type"] not in VALID_TRANSACTION_FILTERS:
        filters["transaction_type"] = "all"
        messages.append("Invalid transaction type filter was ignored.")

    if filters["sort"] not in DASHBOARD_SORT_OPTIONS:
        filters["sort"] = DEFAULT_DASHBOARD_SORT
        messages.append("Invalid sort option was ignored.")

    return filters, messages


def apply_transaction_sort(query, sort_key):
    if sort_key == "oldest":
        return query.order_by(Transaction.date.asc(), Transaction.id.asc())
    if sort_key == "highest":
        return query.order_by(Transaction.amount.desc(), Transaction.date.desc(), Transaction.id.desc())
    if sort_key == "lowest":
        return query.order_by(Transaction.amount.asc(), Transaction.date.desc(), Transaction.id.desc())

    return query.order_by(Transaction.date.desc(), Transaction.id.desc())


def apply_transaction_filters(query, filters):
    applied_filters = dict(filters)
    messages = []

    if applied_filters["filter_date"]:
        try:
            selected_date = datetime.strptime(applied_filters["filter_date"], "%Y-%m-%d").date()
            query = query.filter(func.date(Transaction.date) == selected_date.isoformat())
        except ValueError:
            applied_filters["filter_date"] = ""
            messages.append("Invalid input. Date filter was ignored.")
    elif applied_filters["filter_month"]:
        try:
            month_start = datetime.strptime(applied_filters["filter_month"], "%Y-%m")
            if month_start.month == 12:
                month_end = datetime(month_start.year + 1, 1, 1)
            else:
                month_end = datetime(month_start.year, month_start.month + 1, 1)
            query = query.filter(Transaction.date >= month_start, Transaction.date < month_end)
        except ValueError:
            applied_filters["filter_month"] = ""
            messages.append("Invalid input. Month filter was ignored.")

    if applied_filters["transaction_type"] in VALID_TRANSACTION_TYPES:
        query = query.filter(Transaction.type == applied_filters["transaction_type"])

    if applied_filters["category"]:
        query = query.filter(func.lower(Transaction.category) == applied_filters["category"].lower())

    if applied_filters["search"]:
        pattern = f"%{applied_filters['search']}%"
        query = query.filter(
            or_(
                Transaction.type.ilike(pattern),
                Transaction.category.ilike(pattern),
                Transaction.description.ilike(pattern),
                Transaction.tags.ilike(pattern),
            )
        )

    query = apply_transaction_sort(query, applied_filters["sort"])
    return query, applied_filters, messages


def build_category_options(transactions, selected_category=""):
    seen_categories = {}

    for transaction in transactions:
        category_name = (transaction.category or "").strip()
        if not category_name:
            continue
        seen_categories.setdefault(category_name.lower(), category_name)

    if selected_category:
        seen_categories.setdefault(selected_category.lower(), selected_category)

    return [seen_categories[key] for key in sorted(seen_categories)]


def build_active_filter_labels(filters):
    labels = []

    if filters["filter_date"]:
        labels.append(f"Date: {filters['filter_date']}")
    elif filters["filter_month"]:
        labels.append(f"Month: {filters['filter_month']}")

    if filters["search"]:
        labels.append(f"Search: {filters['search']}")
    if filters["transaction_type"] != "all":
        labels.append(f"Type: {filters['transaction_type'].title()}")
    if filters["category"]:
        labels.append(f"Category: {filters['category']}")
    if filters["sort"] != DEFAULT_DASHBOARD_SORT:
        labels.append(DASHBOARD_SORT_OPTIONS[filters["sort"]])

    return labels


def build_monthly_snapshot(transactions, month, year, budget=None):
    monthly_transactions = [
        transaction
        for transaction in transactions
        if transaction.date and transaction.date.month == month and transaction.date.year == year
    ]
    expense_transactions = [
        transaction for transaction in monthly_transactions if transaction.type == "expense"
    ]
    income_transactions = [
        transaction for transaction in monthly_transactions if transaction.type == "income"
    ]
    expense_total = round(sum(transaction.amount for transaction in expense_transactions), 2)
    income_total = round(sum(transaction.amount for transaction in income_transactions), 2)
    net_total = round(income_total - expense_total, 2)

    daily_spending = {}
    for transaction in expense_transactions:
        date_key = transaction.date.strftime("%Y-%m-%d")
        daily_spending[date_key] = round(daily_spending.get(date_key, 0.0) + transaction.amount, 2)

    active_days = len(daily_spending)
    average_active_day_spend = round(expense_total / active_days, 2) if active_days else 0.0

    top_spending_day_label = None
    top_spending_day_amount = 0.0
    if daily_spending:
        top_spending_day = max(daily_spending, key=daily_spending.get)
        top_spending_day_label = datetime.strptime(top_spending_day, "%Y-%m-%d").strftime("%b %d")
        top_spending_day_amount = round(daily_spending[top_spending_day], 2)

    largest_expense = max(expense_transactions, key=lambda transaction: transaction.amount, default=None)
    largest_expense_summary = None
    if largest_expense is not None:
        largest_expense_summary = {
            "amount": round(largest_expense.amount, 2),
            "category": largest_expense.category or "Miscellaneous",
            "date_label": largest_expense.date.strftime("%b %d"),
        }

    tag_counter = Counter()
    tag_labels = {}
    for transaction in monthly_transactions:
        for raw_tag in (transaction.tags or "").split(","):
            cleaned_tag = raw_tag.strip()
            if not cleaned_tag:
                continue
            normalized_tag = cleaned_tag.lower()
            tag_counter[normalized_tag] += 1
            tag_labels.setdefault(normalized_tag, cleaned_tag)

    top_tag = None
    if tag_counter:
        top_tag_key, top_tag_count = tag_counter.most_common(1)[0]
        top_tag = {
            "label": tag_labels[top_tag_key],
            "count": top_tag_count,
        }

    days_in_month = calendar.monthrange(year, month)[1]
    today = utc_now().date()
    elapsed_days = today.day if today.year == year and today.month == month else days_in_month
    projected_month_end_expense = (
        round((expense_total / max(elapsed_days, 1)) * days_in_month, 2) if expense_total else 0.0
    )

    budget_amount = round(budget.amount, 2) if budget else None
    budget_used_percentage = None
    projected_budget_gap = None
    if budget_amount:
        budget_used_percentage = round((expense_total / budget_amount) * 100, 2)
        projected_budget_gap = round(budget_amount - projected_month_end_expense, 2)

    return {
        "month": month,
        "year": year,
        "month_label": f"{calendar.month_name[month]} {year}",
        "transaction_count": len(monthly_transactions),
        "income_total": income_total,
        "expense_total": expense_total,
        "net_total": net_total,
        "active_days": active_days,
        "average_active_day_spend": average_active_day_spend,
        "top_spending_day_label": top_spending_day_label,
        "top_spending_day_amount": top_spending_day_amount,
        "largest_expense": largest_expense_summary,
        "top_tag": top_tag,
        "projected_month_end_expense": projected_month_end_expense,
        "budget_amount": budget_amount,
        "budget_used_percentage": budget_used_percentage,
        "projected_budget_gap": projected_budget_gap,
    }


def build_dashboard_redirect_url(
    base_path="/",
    filter_date="",
    filter_month="",
    month=None,
    year=None,
    search="",
    transaction_type="all",
    category="",
    sort=DEFAULT_DASHBOARD_SORT,
):
    if base_path not in {"/", "/dashboard", "/download_csv"}:
        base_path = "/"

    query_params = {}

    if filter_date:
        query_params["filter_date"] = filter_date
    if filter_month:
        query_params["filter_month"] = filter_month
    if search:
        query_params["search"] = search
    if transaction_type and transaction_type != "all":
        query_params["transaction_type"] = transaction_type
    if category:
        query_params["category"] = category
    if sort and sort != DEFAULT_DASHBOARD_SORT:
        query_params["sort"] = sort
    if month:
        query_params["month"] = month
    if year:
        query_params["year"] = year

    if not query_params:
        return base_path

    return f"{base_path}?{urlencode(query_params)}"


def build_spending_heatmap(user_id, month, year, date_map=None):
    first_weekday, num_days = calendar.monthrange(year, month)
    if date_map is None:
        grouped_expenses = (
            db.session.query(
                func.date(Transaction.date).label("expense_date"),
                func.sum(Transaction.amount).label("daily_total"),
            )
            .filter(
                Transaction.user_id == user_id,
                Transaction.type == "expense",
                func.strftime("%m", Transaction.date) == f"{month:02d}",
                func.strftime("%Y", Transaction.date) == str(year),
            )
            .group_by(func.date(Transaction.date))
            .all()
        )
        date_map = {
            row.expense_date: round(float(row.daily_total or 0.0), 2) for row in grouped_expenses
        }
    max_total = max(date_map.values(), default=0.0)

    if month == 1:
        prev_month = 12
        prev_year = year - 1
    else:
        prev_month = month - 1
        prev_year = year

    if month == 12:
        next_month = 1
        next_year = year + 1
    else:
        next_month = month + 1
        next_year = year

    cells = [{"is_padding": True} for _ in range(first_weekday)]
    for day in range(1, num_days + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        amount = date_map.get(date_str, 0.0)

        if amount == 0:
            color = "#020617"
            text_color = "#94a3b8"
        elif amount < 500:
            color = "#14532d"
            text_color = "#dcfce7"
        elif amount < 2000:
            color = "#22c55e"
            text_color = "#052e16"
        else:
            color = "#4ade80"
            text_color = "#052e16"

        cells.append(
            {
                "is_padding": False,
                "day": day,
                "date_label": date_str,
                "amount": amount,
                "color": color,
                "text_color": text_color,
            }
        )

    while len(cells) % 7 != 0:
        cells.append({"is_padding": True})

    return {
        "weekday_labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "cells": cells,
        "month": month,
        "year": year,
        "month_name": calendar.month_name[month],
        "prev_month": prev_month,
        "prev_year": prev_year,
        "next_month": next_month,
        "next_year": next_year,
        "max_total": round(max_total, 2),
    }


def get_recent_transactions_for_report(user_id, limit=20):
    return (
        Transaction.query.filter_by(user_id=user_id)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )


def build_report_rows(transactions, final_balance):
    running_balance = round(final_balance, 2)
    report_rows = []

    for transaction in transactions:
        report_rows.append(
            {
                "date": transaction.date.strftime("%Y-%m-%d") if transaction.date else "Unknown",
                "type": transaction.type.title(),
                "category": transaction.category or "Miscellaneous",
                "amount": round(transaction.amount, 2),
                "balance_after": round(running_balance, 2),
            }
        )

        if transaction.type == "expense":
            running_balance = round(running_balance + transaction.amount, 2)
        else:
            running_balance = round(running_balance - transaction.amount, 2)

    return report_rows


def build_pdf_report_elements(report_rows, total_income, total_expense, balance):
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph("ExpenseStats Report", styles["Title"]))
    elements.append(Spacer(1, 12))
    elements.append(Paragraph("Summary", styles["Heading2"]))
    elements.append(Spacer(1, 8))

    summary_table = Table(
        [
            ["Total Income", f"Rs {total_income:.2f}"],
            ["Total Expense", f"Rs {total_expense:.2f}"],
            ["Final Balance", f"Rs {balance:.2f}"],
        ],
        colWidths=[170, 130],
        hAlign="LEFT",
    )
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#0f172a")),
                ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor("#cbd5e1")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    elements.append(summary_table)
    elements.append(Spacer(1, 18))
    elements.append(Paragraph("Last 20 Transactions", styles["Heading2"]))
    elements.append(Spacer(1, 8))

    table_data = [["Date", "Type", "Category", "Amount", "Balance After"]]
    for row in report_rows:
        table_data.append(
            [
                row["date"],
                row["type"],
                row["category"],
                f"Rs {row['amount']:.2f}",
                f"Rs {row['balance_after']:.2f}",
            ]
        )

    if len(table_data) == 1:
        elements.append(Paragraph("No transactions available.", styles["Normal"]))
        return elements

    transactions_table = Table(
        table_data,
        colWidths=[80, 68, 132, 90, 110],
        repeatRows=1,
        hAlign="LEFT",
    )
    transactions_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f8fafc")),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#111827")),
                ("ALIGN", (0, 0), (2, -1), "LEFT"),
                ("ALIGN", (3, 0), (4, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    elements.append(transactions_table)
    return elements


def generate_pdf_report(report_rows, total_income, total_expense, balance):
    report_path = os.path.join(app.root_path, "report.pdf")
    document = SimpleDocTemplate(report_path, pagesize=letter, leftMargin=36, rightMargin=36)
    elements = build_pdf_report_elements(report_rows, total_income, total_expense, balance)
    document.build(elements)
    return report_path


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect("/")

    if request.method == "POST":
        username = normalize_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not password or not confirm_password:
            flash("Invalid input. Username, password, and confirm password are required.", "error")
            return render_template("register.html", error=None)

        if len(password) < 8:
            flash("Invalid input. Password must be at least 8 characters.", "error")
            return render_template("register.html", error=None)

        if password != confirm_password:
            flash("Invalid input. Password and confirm password must match.", "error")
            return render_template("register.html", error=None)

        try:
            existing_user = User.query.filter(func.lower(User.username) == username).first()
            if existing_user:
                flash("Invalid input. Username already exists.", "error")
                return render_template("register.html", error=None)

            hashed_password = generate_password_hash(password)
            user = User(username=username, password=hashed_password)
            db.session.add(user)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            error_text = str(getattr(exc, "orig", exc)).lower()
            if "unique constraint failed: user.username" in error_text:
                flash("Invalid input. Username already exists.", "error")
            else:
                app.logger.warning(
                    "Account creation integrity error for username %s: %s", username, exc
                )
                flash("Something went wrong while creating the account. Please try again.", "error")
            return render_template("register.html", error=None)
        except OperationalError as exc:
            db.session.rollback()
            error_text = str(getattr(exc, "orig", exc)).lower()
            if "database is locked" in error_text:
                flash("The database is busy right now. Please try again in a moment.", "error")
            else:
                flash("Something went wrong while creating the account. Please try again.", "error")
            app.logger.warning("Account creation operational error for username %s: %s", username, exc)
            return render_template("register.html", error=None)
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Account creation failed for username %s", username)
            flash("Something went wrong while creating the account. Please try again.", "error")
            return render_template("register.html", error=None)

        flash("Registration successful. Please log in.", "success")
        return redirect("/login")

    return render_template("register.html", error=None)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect("/")

    if request.method == "POST":
        username = normalize_username(request.form.get("username", ""))
        password = request.form.get("password", "")

        if not username or not password:
            flash("Invalid input. Username and password are required.", "error")
            return render_template("login.html", error=None)

        try:
            user = User.query.filter(func.lower(User.username) == username).first()
        except SQLAlchemyError:
            flash("Something went wrong. Please try again.", "error")
            return render_template("login.html", error=None)

        if not user:
            flash("Invalid input. Username or password is incorrect.", "error")
            return render_template("login.html", error=None)

        password_matches, upgrade_message = verify_user_password(user, password)
        if password_matches:
            session["user_id"] = user.id
            if upgrade_message:
                flash(upgrade_message, "success")
            return redirect("/")

        flash(upgrade_message or "Invalid input. Username or password is incorrect.", "error")
        return render_template("login.html", error=None)

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/set_budget", methods=["POST"])
def set_budget():
    if "user_id" not in session:
        return redirect("/login")

    dashboard_path = request.form.get("dashboard_path", "/")
    filter_date = request.form.get("filter_date", "")
    filter_month = request.form.get("filter_month", "")
    search = request.form.get("search", "")
    transaction_type = request.form.get("transaction_type", "all")
    category = request.form.get("category", "")
    sort = request.form.get("sort", DEFAULT_DASHBOARD_SORT)
    dashboard_month = request.form.get("month", "")
    dashboard_year = request.form.get("year", "")
    dashboard_url = build_dashboard_redirect_url(
        base_path=dashboard_path,
        filter_date=filter_date,
        filter_month=filter_month,
        search=search,
        transaction_type=transaction_type,
        category=category,
        sort=sort,
        month=dashboard_month,
        year=dashboard_year,
    )
    budget_amount = request.form.get("budget_amount", "").strip()
    if not budget_amount:
        flash("Invalid input. Budget amount is required.", "error")
        return redirect(dashboard_url)

    try:
        amount = float(budget_amount)
    except ValueError:
        flash("Invalid input. Budget must be a number.", "error")
        return redirect(dashboard_url)

    if amount <= 0:
        flash("Invalid input. Budget must be greater than 0.", "error")
        return redirect(dashboard_url)

    try:
        budget = Budget.query.filter_by(user_id=session["user_id"]).first()
        if budget:
            budget.amount = amount
        else:
            budget = Budget(user_id=session["user_id"], amount=amount)
            db.session.add(budget)

        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Something went wrong. Please try again.", "error")
        return redirect(dashboard_url)

    flash("Budget saved successfully.", "success")
    return redirect(dashboard_url)


@app.route("/dashboard")
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")

    filters, filter_messages = normalize_dashboard_filters(request.args)
    for message in filter_messages:
        flash(message, "error")

    today = utc_now().date()

    try:
        heatmap_month = int(request.args.get("month", today.month))
        heatmap_year = int(request.args.get("year", today.year))
        if heatmap_month < 1 or heatmap_month > 12 or heatmap_year < 1:
            raise ValueError
    except ValueError:
        heatmap_month = today.month
        heatmap_year = today.year

    try:
        user_id = session["user_id"]
        all_transactions = Transaction.query.filter_by(user_id=user_id).all()
        all_expense_transactions = Transaction.query.filter_by(
            user_id=session["user_id"], type="expense"
        ).all()
        query = Transaction.query.filter_by(user_id=user_id)
        query, filters, filter_application_messages = apply_transaction_filters(query, filters)
        for message in filter_application_messages:
            flash(message, "error")

        transactions = query.all()
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
        budget = Budget.query.filter_by(user_id=user_id).first()
        monthly_snapshot = build_monthly_snapshot(all_transactions, heatmap_month, heatmap_year, budget)
        remaining_budget = (
            round(budget.amount - monthly_snapshot["expense_total"], 2) if budget else None
        )
        budget_exceeded = budget is not None and monthly_snapshot["expense_total"] > budget.amount
        insights = build_insights(expense_transactions, all_expense_transactions)
        future_projection = calculate_future_projection(all_transactions)
        spending_heatmap = build_spending_heatmap(user_id, heatmap_month, heatmap_year)
        category_options = build_category_options(all_transactions, filters["category"])
        active_filters = build_active_filter_labels(filters)
        download_csv_url = build_dashboard_redirect_url(
            base_path="/download_csv",
            filter_date=filters["filter_date"],
            filter_month=filters["filter_month"],
            search=filters["search"],
            transaction_type=filters["transaction_type"],
            category=filters["category"],
            sort=filters["sort"],
            month=heatmap_month,
            year=heatmap_year,
        )
        dashboard_clear_url = build_dashboard_redirect_url(
            base_path=request.path,
            month=heatmap_month,
            year=heatmap_year,
        )
        heatmap_prev_url = build_dashboard_redirect_url(
            base_path=request.path,
            filter_date=filters["filter_date"],
            filter_month=filters["filter_month"],
            search=filters["search"],
            transaction_type=filters["transaction_type"],
            category=filters["category"],
            sort=filters["sort"],
            month=spending_heatmap["prev_month"],
            year=spending_heatmap["prev_year"],
        )
        heatmap_next_url = build_dashboard_redirect_url(
            base_path=request.path,
            filter_date=filters["filter_date"],
            filter_month=filters["filter_month"],
            search=filters["search"],
            transaction_type=filters["transaction_type"],
            category=filters["category"],
            sort=filters["sort"],
            month=spending_heatmap["next_month"],
            year=spending_heatmap["next_year"],
        )
        generate_donut_chart(expense_transactions, balance)
        generate_trend_chart(expense_transactions)
    except Exception:
        flash("Something went wrong while loading your dashboard.", "error")
        transactions = []
        total_income = 0
        total_expense = 0
        balance = 0
        savings_rate = 0
        budget = None
        remaining_budget = None
        budget_exceeded = False
        insights = ["Something went wrong."]
        future_projection = calculate_future_projection([])
        monthly_snapshot = build_monthly_snapshot([], heatmap_month, heatmap_year)
        spending_heatmap = build_spending_heatmap(
            None,
            heatmap_month,
            heatmap_year,
            date_map={},
        )
        category_options = build_category_options([], filters["category"])
        active_filters = build_active_filter_labels(filters)
        download_csv_url = build_dashboard_redirect_url(
            base_path="/download_csv",
            filter_date=filters["filter_date"],
            filter_month=filters["filter_month"],
            search=filters["search"],
            transaction_type=filters["transaction_type"],
            category=filters["category"],
            sort=filters["sort"],
            month=heatmap_month,
            year=heatmap_year,
        )
        dashboard_clear_url = build_dashboard_redirect_url(
            base_path=request.path,
            month=heatmap_month,
            year=heatmap_year,
        )
        heatmap_prev_url = build_dashboard_redirect_url(
            base_path=request.path,
            filter_date=filters["filter_date"],
            filter_month=filters["filter_month"],
            search=filters["search"],
            transaction_type=filters["transaction_type"],
            category=filters["category"],
            sort=filters["sort"],
            month=spending_heatmap["prev_month"],
            year=spending_heatmap["prev_year"],
        )
        heatmap_next_url = build_dashboard_redirect_url(
            base_path=request.path,
            filter_date=filters["filter_date"],
            filter_month=filters["filter_month"],
            search=filters["search"],
            transaction_type=filters["transaction_type"],
            category=filters["category"],
            sort=filters["sort"],
            month=spending_heatmap["next_month"],
            year=spending_heatmap["next_year"],
        )

    return render_template(
        "index.html",
        transactions=transactions,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        savings_rate=savings_rate,
        filters=filters,
        filter_date=filters["filter_date"],
        filter_month=filters["filter_month"],
        budget=budget,
        remaining_budget=remaining_budget,
        budget_exceeded=budget_exceeded,
        insights=insights,
        future_projection=future_projection,
        monthly_snapshot=monthly_snapshot,
        spending_heatmap=spending_heatmap,
        category_options=category_options,
        sort_options=DASHBOARD_SORT_OPTIONS,
        active_filters=active_filters,
        download_csv_url=download_csv_url,
        dashboard_clear_url=dashboard_clear_url,
        heatmap_prev_url=heatmap_prev_url,
        heatmap_next_url=heatmap_next_url,
    )


@app.route("/calculate", methods=["POST"])
def calculate_amount():
    if "user_id" not in session:
        return jsonify({"error": "Login required."}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form

    if not hasattr(payload, "get"):
        return jsonify({"error": "Invalid calculation."}), 400

    expression = str(payload.get("expression", "")).strip()

    try:
        result = evaluate_calculation_expression(expression)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"result": result, "formatted_result": f"{result:.2f}"})


@app.route("/add", methods=["GET", "POST"])
def add_transaction():
    if "user_id" not in session:
        return redirect("/login")

    today = datetime.now().strftime("%Y-%m-%d")
    t_type = request.args.get("type", "expense")
    if t_type not in VALID_TRANSACTION_TYPES:
        t_type = "expense"

    if request.method == "POST":
        t_type = request.form.get("type", "expense")
        transaction_data, error_message = parse_transaction_form(request.form)
        if error_message:
            flash(error_message, "error")
            return render_template("add.html", today=today, type=t_type)

        try:
            transaction = Transaction(
                amount=transaction_data["amount"],
                type=transaction_data["type"],
                category=transaction_data["category"],
                description=transaction_data["description"],
                tags=transaction_data["tags"],
                user_id=session["user_id"],
                date=transaction_data["date"],
            )
            db.session.add(transaction)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Something went wrong. Please try again.", "error")
            return render_template("add.html", today=today, type=t_type)

        flash("Transaction saved successfully.", "success")
        return redirect("/")

    return render_template("add.html", today=today, type=t_type)


@app.route("/delete/<int:id>")
def delete_transaction(id):
    if "user_id" not in session:
        return redirect("/login")

    try:
        transaction = Transaction.query.filter_by(id=id, user_id=session["user_id"]).first()
        if transaction:
            db.session.delete(transaction)
            db.session.commit()
            flash("Transaction deleted successfully.", "success")
        else:
            flash("Invalid input. Transaction not found.", "error")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Something went wrong. Please try again.", "error")
    return redirect("/")


@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_transaction(id):
    if "user_id" not in session:
        return redirect("/login")

    try:
        transaction = Transaction.query.filter_by(id=id, user_id=session["user_id"]).first()
    except SQLAlchemyError:
        flash("Something went wrong. Please try again.", "error")
        return redirect("/")

    if not transaction:
        flash("Invalid input. Transaction not found.", "error")
        return redirect("/")

    if request.method == "POST":
        transaction_data, error_message = parse_transaction_form(
            request.form, default_date=transaction.date or utc_now()
        )
        if error_message:
            flash(error_message, "error")
            return render_template("edit.html", transaction=transaction)

        try:
            transaction.amount = transaction_data["amount"]
            transaction.type = transaction_data["type"]
            transaction.category = transaction_data["category"]
            transaction.description = transaction_data["description"]
            transaction.tags = transaction_data["tags"]
            transaction.date = transaction_data["date"]
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Something went wrong. Please try again.", "error")
            return render_template("edit.html", transaction=transaction)

        flash("Transaction updated successfully.", "success")
        return redirect("/")

    return render_template("edit.html", transaction=transaction)


@app.route("/chart")
def chart():
    if "user_id" not in session:
        return redirect("/login")

    try:
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
    except Exception:
        flash("Something went wrong while loading charts.", "error")
        return redirect("/")

    return render_template("chart.html")


@app.route("/about")
def about():
    if "user_id" not in session:
        return redirect("/login")

    return render_template("about.html")


@app.route("/download_csv")
def download_csv():
    if "user_id" not in session:
        return redirect("/login")

    filters, _ = normalize_dashboard_filters(request.args)

    try:
        query = Transaction.query.filter_by(user_id=session["user_id"])
        query, filters, _ = apply_transaction_filters(query, filters)
        transactions = query.all()
    except SQLAlchemyError:
        flash("Something went wrong while generating the CSV export.", "error")
        return redirect(
            build_dashboard_redirect_url(
                base_path="/",
                filter_date=filters["filter_date"],
                filter_month=filters["filter_month"],
                search=filters["search"],
                transaction_type=filters["transaction_type"],
                category=filters["category"],
                sort=filters["sort"],
            )
        )

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Type", "Category", "Description", "Tags", "Amount"])

    for transaction in transactions:
        writer.writerow(
            [
                transaction.date.strftime("%Y-%m-%d") if transaction.date else "",
                transaction.type.title(),
                transaction.category or "Miscellaneous",
                transaction.description or "",
                transaction.tags or "",
                f"{transaction.amount:.2f}",
            ]
        )

    response = make_response(output.getvalue())
    response.mimetype = "text/csv"
    response.headers["Content-Disposition"] = "attachment; filename=transactions-export.csv"
    return response


@app.route("/download_report")
def download_report():
    if "user_id" not in session:
        return redirect("/login")

    try:
        user_id = session["user_id"]
        report_transactions = get_recent_transactions_for_report(user_id)
        total_income = (
            db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
            .filter_by(user_id=user_id, type="income")
            .scalar()
        )
        total_expense = (
            db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
            .filter_by(user_id=user_id, type="expense")
            .scalar()
        )
        balance = total_income - total_expense
        report_rows = build_report_rows(report_transactions, balance)

        report_path = generate_pdf_report(report_rows, total_income, total_expense, balance)
    except Exception:
        flash("Something went wrong while generating the report.", "error")
        return redirect("/")

    return send_file(report_path, as_attachment=True, download_name="report.pdf")


if __name__ == "__main__":
    app.run(debug=os.environ.get('FLASK_ENV') != 'production')
