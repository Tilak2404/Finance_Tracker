import calendar
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.security import check_password_hash

from app import (
    Budget,
    Transaction,
    User,
    app,
    build_spending_heatmap,
    calculate_future_projection,
    build_pdf_report_elements,
    build_report_rows,
    db,
    get_recent_transactions_for_report,
)


class FinanceTrackerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{cls.db_path}",
        )

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
            db.drop_all()

        os.close(cls.db_fd)
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)

    def setUp(self):
        self.client = app.test_client()
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()

    def register(self, username="alice", password="secret123"):
        return self.client.post(
            "/register",
            data={
                "username": username,
                "password": password,
                "confirm_password": password,
            },
            follow_redirects=True,
        )

    def login(self, username="alice", password="secret123"):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def create_user(self, username, password="secret123"):
        user = User(username=username, password=password)
        with app.app_context():
            if not password.startswith("pbkdf2:") and not password.startswith("scrypt:"):
                from werkzeug.security import generate_password_hash

                user.password = generate_password_hash(password)
            db.session.add(user)
            db.session.commit()
            return user.id

    def create_transaction(
        self,
        user_id,
        amount,
        transaction_type,
        category="",
        description="",
        tags="",
        when=None,
    ):
        when = when or datetime.now(UTC).replace(tzinfo=None)
        with app.app_context():
            transaction = Transaction(
                amount=amount,
                type=transaction_type,
                category=category,
                description=description,
                tags=tags,
                user_id=user_id,
                date=when,
            )
            db.session.add(transaction)
            db.session.commit()
            return transaction.id

    def test_protected_routes_redirect_to_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

        for path in [
            "/add",
            "/about",
            "/chart",
            "/dashboard",
            "/download_csv",
            "/download_report",
            "/edit/1",
            "/delete/1",
        ]:
            route_response = self.client.get(path, follow_redirects=False)
            self.assertEqual(route_response.status_code, 302, path)
            self.assertTrue(route_response.headers["Location"].endswith("/login"), path)

        budget_response = self.client.post("/set_budget", data={}, follow_redirects=False)
        self.assertEqual(budget_response.status_code, 302)
        self.assertTrue(budget_response.headers["Location"].endswith("/login"))

    def test_favicon_is_available_without_login(self):
        response = self.client.get("/favicon.ico", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("image/svg+xml", response.headers["Content-Type"])

        login_page = self.client.get("/login", follow_redirects=True)
        self.assertIn('rel="icon"', login_page.get_data(as_text=True))
        response.close()
        login_page.close()

    def test_register_hashes_password_and_login_logout_flow(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Registration successful", response.get_data(as_text=True))

        with app.app_context():
            user = User.query.filter_by(username="alice").first()
            self.assertIsNotNone(user)
            self.assertEqual(user.username, "alice")
            self.assertNotEqual(user.password, "secret123")
            self.assertTrue(check_password_hash(user.password, "secret123"))

        duplicate = self.register()
        self.assertIn("Username already exists", duplicate.get_data(as_text=True))

        mismatched = self.client.post(
            "/register",
            data={
                "username": "charlie",
                "password": "secret123",
                "confirm_password": "different123",
            },
            follow_redirects=True,
        )
        self.assertIn("confirm password must match", mismatched.get_data(as_text=True))

        short_password = self.client.post(
            "/register",
            data={
                "username": "dave",
                "password": "short",
                "confirm_password": "short",
            },
            follow_redirects=True,
        )
        self.assertIn("Password must be at least 8 characters", short_password.get_data(as_text=True))

        bad_login = self.login(password="wrong-password")
        self.assertIn("Username or password is incorrect", bad_login.get_data(as_text=True))

        login_response = self.login()
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("Transactions", login_response.get_data(as_text=True))

        logout_response = self.client.get("/logout", follow_redirects=True)
        self.assertEqual(logout_response.status_code, 200)
        self.assertIn("Login", logout_response.get_data(as_text=True))

    def test_login_and_registration_are_case_insensitive_for_usernames(self):
        response = self.client.post(
            "/register",
            data={
                "username": "Alice",
                "password": "secret123",
                "confirm_password": "secret123",
            },
            follow_redirects=True,
        )
        self.assertIn("Registration successful", response.get_data(as_text=True))

        with app.app_context():
            user = User.query.filter_by(username="alice").first()
            self.assertIsNotNone(user)

        duplicate = self.client.post(
            "/register",
            data={
                "username": "ALICE",
                "password": "secret123",
                "confirm_password": "secret123",
            },
            follow_redirects=True,
        )
        self.assertIn("Username already exists", duplicate.get_data(as_text=True))

        login_response = self.login("ALICE", "secret123")
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("ExpenseStats", login_response.get_data(as_text=True))

    def test_register_handles_duplicate_username_integrity_error(self):
        duplicate_error = IntegrityError(
            "INSERT INTO user (username, password) VALUES (?, ?)",
            ("alice", "secret123"),
            Exception("UNIQUE constraint failed: user.username"),
        )

        with patch("app.db.session.commit", side_effect=duplicate_error):
            response = self.client.post(
                "/register",
                data={
                    "username": "alice",
                    "password": "secret123",
                    "confirm_password": "secret123",
                },
                follow_redirects=True,
            )

        self.assertIn("Username already exists", response.get_data(as_text=True))

    def test_register_handles_database_locked_error(self):
        locked_error = OperationalError(
            "INSERT INTO user (username, password) VALUES (?, ?)",
            ("alice", "secret123"),
            Exception("database is locked"),
        )

        with patch("app.db.session.commit", side_effect=locked_error):
            response = self.client.post(
                "/register",
                data={
                    "username": "alice",
                    "password": "secret123",
                    "confirm_password": "secret123",
                },
                follow_redirects=True,
            )

        self.assertIn("database is busy right now", response.get_data(as_text=True).lower())

    def test_add_transaction_validation_and_success(self):
        self.register()
        self.login()

        add_page = self.client.get("/add", follow_redirects=True)
        add_page_text = add_page.get_data(as_text=True)
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), add_page_text)
        self.assertIn('name="type" value="expense"', add_page_text)
        self.assertIn('name="category"', add_page_text)

        income_add_page = self.client.get("/add?type=income", follow_redirects=True)
        income_page_text = income_add_page.get_data(as_text=True)
        self.assertIn('name="type" value="income"', income_page_text)
        self.assertNotIn('id="category"', income_page_text)

        invalid_amount = self.client.post(
            "/add",
            data={"amount": "-10", "type": "expense", "category": "Food"},
            follow_redirects=True,
        )
        self.assertIn("Amount must be greater than 0", invalid_amount.get_data(as_text=True))

        invalid_type = self.client.post(
            "/add",
            data={"amount": "10", "type": "bonus", "category": "Salary"},
            follow_redirects=True,
        )
        self.assertIn("Type must be income or expense", invalid_type.get_data(as_text=True))

        missing_category = self.client.post(
            "/add",
            data={"amount": "10", "type": "expense", "category": ""},
            follow_redirects=True,
        )
        self.assertIn("Category is required", missing_category.get_data(as_text=True))

        success = self.client.post(
            "/add",
            data={
                "date": "2026-03-20",
                "amount": "1250.50",
                "type": "expense",
                "category": "Food",
                "description": "Groceries",
                "tags": "weekly, essentials",
            },
            follow_redirects=True,
        )
        self.assertIn("Transaction saved successfully", success.get_data(as_text=True))
        self.assertIn("essentials", success.get_data(as_text=True))

        with app.app_context():
            transaction = Transaction.query.one()
            self.assertEqual(transaction.amount, 1250.50)
            self.assertEqual(transaction.type, "expense")
            self.assertEqual(transaction.tags, "weekly, essentials")
            self.assertEqual(transaction.user_id, User.query.filter_by(username="alice").first().id)
            self.assertEqual(transaction.date.strftime("%Y-%m-%d"), "2026-03-20")

        income_success = self.client.post(
            "/add",
            data={
                "date": "2026-03-21",
                "amount": "5000",
                "type": "income",
                "description": "Salary credit",
                "tags": "monthly",
            },
            follow_redirects=True,
        )
        self.assertIn("Transaction saved successfully", income_success.get_data(as_text=True))

        with app.app_context():
            income_transaction = (
                Transaction.query.filter_by(type="income").order_by(Transaction.id.desc()).first()
            )
            self.assertIsNotNone(income_transaction)
            self.assertEqual(income_transaction.category, "Income")

    def test_add_page_calculator_markup_and_calculation_endpoint(self):
        self.register()
        self.login()

        add_page = self.client.get("/add", follow_redirects=True)
        add_page_text = add_page.get_data(as_text=True)
        self.assertIn('id="calculator-toggle"', add_page_text)
        self.assertIn('id="calculator-modal"', add_page_text)
        self.assertIn('fetch("/calculate"', add_page_text)

        calculation = self.client.post("/calculate", json={"expression": "12.5 + 7.5 * 2"})
        self.assertEqual(calculation.status_code, 200)
        self.assertEqual(calculation.get_json()["formatted_result"], "27.50")

        invalid_calculation = self.client.post("/calculate", json={"expression": "abs(10)"})
        self.assertEqual(invalid_calculation.status_code, 400)
        self.assertIn("Invalid calculation", invalid_calculation.get_json()["error"])

        zero_division = self.client.post("/calculate", json={"expression": "25 / 0"})
        self.assertEqual(zero_division.status_code, 400)
        self.assertIn("Division by zero", zero_division.get_json()["error"])

    def test_calculator_endpoint_requires_login(self):
        response = self.client.post("/calculate", json={"expression": "2 + 2"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Login required.")

    def test_dashboard_isolation_filters_savings_and_budget(self):
        user_id = self.create_user("alice")
        other_user_id = self.create_user("bob")
        today = datetime(2026, 3, 20)
        last_week = today - timedelta(days=7)
        last_month = datetime(2026, 2, 15)

        self.create_transaction(user_id, 4000, "income", "Salary", tags="monthly", when=today)
        self.create_transaction(user_id, 1000, "expense", "Food", tags="lunch", when=today)
        self.create_transaction(user_id, 500, "expense", "Transport", tags="cab", when=last_week)
        self.create_transaction(user_id, 300, "expense", "Bills", tags="wifi", when=last_month)
        self.create_transaction(other_user_id, 9999, "expense", "Shopping", when=today)

        with app.app_context():
            db.session.add(Budget(user_id=user_id, amount=1800))
            db.session.commit()

        self.login("alice", "secret123")

        dashboard = self.client.get("/dashboard?month=3&year=2026", follow_redirects=True)
        page = dashboard.get_data(as_text=True)
        self.assertIn("&#8377;4000.00", page)
        self.assertIn("&#8377;1800.00", page)
        self.assertIn("monthly", page)
        self.assertNotIn("9999.00", page)
        self.assertIn("55.00%", page)
        self.assertIn("You spent most on Food.", page)
        self.assertIn("Monthly Snapshot", page)
        self.assertIn("Projected month-end spend", page)
        self.assertIn("Future Projection", page)
        self.assertIn("Spending Heatmap", page)
        self.assertIn("March 2026", page)
        self.assertIn("?month=2&amp;year=2026", page)
        self.assertIn("?month=4&amp;year=2026", page)
        self.assertIn("&#8377;13200.00", page)
        self.assertIn("&#8377;6600.00", page)
        self.assertIn("Peak day: &#8377;1000.00", page)

        filtered_day = self.client.get("/?filter_date=2026-03-20&month=3&year=2026", follow_redirects=True)
        filtered_day_page = filtered_day.get_data(as_text=True)
        self.assertIn("Food", filtered_day_page)
        self.assertIn("lunch", filtered_day_page)
        self.assertNotIn("cab", filtered_day_page)
        self.assertIn("&#8377;1000.00", filtered_day_page)
        self.assertIn("March 2026 Remaining Budget", filtered_day_page)
        self.assertIn("&#8377;300.00", filtered_day_page)

        filtered_month = self.client.get("/?filter_month=2026-02&month=2&year=2026", follow_redirects=True)
        filtered_month_page = filtered_month.get_data(as_text=True)
        self.assertIn("Bills", filtered_month_page)
        self.assertIn("wifi", filtered_month_page)
        self.assertNotIn("lunch", filtered_month_page)
        self.assertIn("&#8377;-300.00", filtered_month_page)
        self.assertIn("February 2026", filtered_month_page)

    def test_dashboard_search_sort_category_and_csv_export(self):
        user_id = self.create_user("alice")
        self.create_transaction(
            user_id,
            5000,
            "income",
            category="Salary",
            description="Main salary credit",
            tags="monthly",
            when=datetime(2026, 3, 1),
        )
        self.create_transaction(
            user_id,
            400,
            "expense",
            category="Transport",
            description="Airport cab",
            tags="travel",
            when=datetime(2026, 3, 3),
        )
        self.create_transaction(
            user_id,
            250,
            "expense",
            category="Food",
            description="Lunch box",
            tags="meal",
            when=datetime(2026, 3, 2),
        )

        self.login("alice", "secret123")

        dashboard = self.client.get(
            "/?search=airport&transaction_type=expense&category=Transport&sort=lowest&month=3&year=2026",
            follow_redirects=True,
        )
        page = dashboard.get_data(as_text=True)
        self.assertIn("Filtered dashboard view", page)
        self.assertIn("Search: airport", page)
        self.assertIn("Type: Expense", page)
        self.assertIn("Category: Transport", page)
        self.assertIn("Airport cab", page)
        self.assertNotIn("Lunch box", page)
        self.assertNotIn("Main salary credit", page)
        self.assertIn("Export Current View", page)

        csv_response = self.client.get(
            "/download_csv?search=airport&transaction_type=expense&category=Transport&sort=lowest&month=3&year=2026",
            follow_redirects=False,
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response.mimetype, "text/csv")
        self.assertIn("transactions-export.csv", csv_response.headers["Content-Disposition"])

        csv_text = csv_response.get_data(as_text=True)
        self.assertIn("Airport cab", csv_text)
        self.assertNotIn("Lunch box", csv_text)
        self.assertNotIn("Main salary credit", csv_text)

    def test_future_projection_helper_averages_monthly_history(self):
        user_id = self.create_user("alice")
        self.create_transaction(user_id, 3000, "income", category="Salary", when=datetime(2026, 1, 5))
        self.create_transaction(user_id, 900, "expense", category="Food", when=datetime(2026, 1, 11))
        self.create_transaction(user_id, 1500, "income", category="Salary", when=datetime(2026, 2, 2))
        self.create_transaction(user_id, 600, "expense", category="Bills", when=datetime(2026, 2, 18))

        with app.app_context():
            transactions = Transaction.query.filter_by(user_id=user_id).all()

        projection = calculate_future_projection(transactions)

        self.assertEqual(projection["average_monthly_income"], 2250.0)
        self.assertEqual(projection["average_monthly_expense"], 750.0)
        self.assertEqual(projection["monthly_savings"], 1500.0)
        self.assertEqual(projection["six_month_projection"], 9000.0)
        self.assertEqual(projection["yearly_savings"], 18000.0)
        self.assertEqual(projection["months_tracked"], 2)

    def test_spending_heatmap_helper_tracks_monthly_calendar_and_colors(self):
        user_id = self.create_user("alice")
        self.create_transaction(user_id, 120, "expense", category="Food", when=datetime(2026, 3, 18))
        self.create_transaction(user_id, 1400, "expense", category="Bills", when=datetime(2026, 3, 20))
        self.create_transaction(user_id, 2200, "expense", category="Transport", when=datetime(2026, 3, 22))

        with app.app_context():
            heatmap = build_spending_heatmap(user_id, 3, 2026)

        active_cells = [cell for cell in heatmap["cells"] if not cell["is_padding"]]
        cells_by_day = {cell["day"]: cell for cell in active_cells}

        self.assertEqual(heatmap["month_name"], "March")
        self.assertEqual(heatmap["prev_month"], 2)
        self.assertEqual(heatmap["prev_year"], 2026)
        self.assertEqual(heatmap["next_month"], 4)
        self.assertEqual(heatmap["next_year"], 2026)
        self.assertEqual(len(active_cells), calendar.monthrange(2026, 3)[1])
        self.assertEqual(heatmap["max_total"], 2200.0)
        self.assertEqual(cells_by_day[1]["color"], "#020617")
        self.assertEqual(cells_by_day[18]["amount"], 120.0)
        self.assertEqual(cells_by_day[18]["color"], "#14532d")
        self.assertEqual(cells_by_day[20]["amount"], 1400.0)
        self.assertEqual(cells_by_day[20]["color"], "#22c55e")
        self.assertEqual(cells_by_day[22]["amount"], 2200.0)
        self.assertEqual(cells_by_day[22]["color"], "#4ade80")

    def test_edit_and_delete_enforce_ownership(self):
        owner_id = self.create_user("alice")
        other_id = self.create_user("bob")
        owner_transaction_id = self.create_transaction(
            owner_id,
            250,
            "expense",
            category="Health",
            description="Medicine",
            tags="urgent",
        )
        other_transaction_id = self.create_transaction(
            other_id,
            900,
            "expense",
            category="Shopping",
            description="Headphones",
            tags="gift",
        )

        self.login("alice", "secret123")

        forbidden_edit = self.client.post(
            f"/edit/{other_transaction_id}",
            data={
                "date": "2026-03-20",
                "amount": "100",
                "type": "expense",
                "category": "Food",
                "description": "Should fail",
                "tags": "blocked",
            },
            follow_redirects=True,
        )
        self.assertIn("Transaction not found", forbidden_edit.get_data(as_text=True))

        allowed_edit = self.client.post(
            f"/edit/{owner_transaction_id}",
            data={
                "date": "2026-03-20",
                "amount": "275",
                "type": "expense",
                "category": "Health",
                "description": "Updated",
                "tags": "urgent,medical",
            },
            follow_redirects=True,
        )
        self.assertIn("Transaction updated successfully", allowed_edit.get_data(as_text=True))

        with app.app_context():
            transaction = db.session.get(Transaction, owner_transaction_id)
            other_transaction = db.session.get(Transaction, other_transaction_id)
            self.assertEqual(transaction.amount, 275)
            self.assertEqual(transaction.tags, "urgent,medical")
            self.assertEqual(other_transaction.description, "Headphones")

        forbidden_delete = self.client.get(f"/delete/{other_transaction_id}", follow_redirects=True)
        self.assertIn("Transaction not found", forbidden_delete.get_data(as_text=True))

        allowed_delete = self.client.get(f"/delete/{owner_transaction_id}", follow_redirects=True)
        self.assertIn("Transaction deleted successfully", allowed_delete.get_data(as_text=True))

        with app.app_context():
            self.assertIsNone(db.session.get(Transaction, owner_transaction_id))
            self.assertIsNotNone(db.session.get(Transaction, other_transaction_id))

    def test_invalid_filters_chart_and_report_routes(self):
        user_id = self.create_user("alice")
        self.create_transaction(
            user_id,
            3000,
            "income",
            category="Salary",
            tags="monthly",
            when=datetime(2026, 3, 1),
        )
        self.create_transaction(
            user_id,
            700,
            "expense",
            category="Entertainment",
            tags="movie",
            when=datetime(2026, 3, 2),
        )

        self.login("alice", "secret123")

        invalid_date = self.client.get("/?filter_date=not-a-date", follow_redirects=True)
        self.assertEqual(invalid_date.status_code, 200)
        self.assertIn("Date filter was ignored", invalid_date.get_data(as_text=True))

        invalid_month = self.client.get("/?filter_month=2026-99", follow_redirects=True)
        self.assertEqual(invalid_month.status_code, 200)
        self.assertIn("Month filter was ignored", invalid_month.get_data(as_text=True))

        chart_path = os.path.join(app.root_path, "static", "chart.png")
        trend_path = os.path.join(app.root_path, "static", "trend.png")
        if os.path.exists(chart_path):
            os.unlink(chart_path)
        if os.path.exists(trend_path):
            os.unlink(trend_path)

        chart_response = self.client.get("/chart", follow_redirects=True)
        self.assertEqual(chart_response.status_code, 200)
        self.assertIn("Expenses by Category", chart_response.get_data(as_text=True))
        self.assertTrue(os.path.exists(chart_path))
        self.assertTrue(os.path.exists(trend_path))
        self.assertGreater(os.path.getsize(chart_path), 0)
        self.assertGreater(os.path.getsize(trend_path), 0)

        report_response = self.client.get("/download_report", follow_redirects=False)
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(report_response.mimetype, "application/pdf")
        report_response.close()

    def test_report_helpers_limit_to_last_20_and_compute_running_balance(self):
        user_id = self.create_user("alice")
        base_date = datetime(2026, 1, 1)
        created_transactions = []

        for offset in range(25):
            transaction_type = "income" if offset % 2 == 0 else "expense"
            amount = float(100 + offset)
            category = "Salary" if transaction_type == "income" else "Food"
            when = base_date + timedelta(days=offset)
            self.create_transaction(user_id, amount, transaction_type, category=category, when=when)
            created_transactions.append((when, transaction_type, amount))

        with app.app_context():
            recent_transactions = get_recent_transactions_for_report(user_id)

        self.assertEqual(len(recent_transactions), 20)
        self.assertEqual(recent_transactions[0].date, base_date + timedelta(days=24))
        self.assertEqual(recent_transactions[-1].date, base_date + timedelta(days=5))

        total_income = sum(amount for _, transaction_type, amount in created_transactions if transaction_type == "income")
        total_expense = sum(amount for _, transaction_type, amount in created_transactions if transaction_type == "expense")
        balance = total_income - total_expense

        report_rows = build_report_rows(recent_transactions, balance)
        running_balance = round(balance, 2)

        for report_row, transaction in zip(report_rows, recent_transactions):
            self.assertEqual(report_row["balance_after"], round(running_balance, 2))
            if transaction.type == "expense":
                running_balance = round(running_balance + transaction.amount, 2)
            else:
                running_balance = round(running_balance - transaction.amount, 2)

    def test_pdf_report_elements_include_summary_and_balance_column(self):
        report_rows = [
            {
                "date": "2026-03-21",
                "type": "Expense",
                "category": "Food",
                "amount": 125.5,
                "balance_after": 980.0,
            },
            {
                "date": "2026-03-20",
                "type": "Income",
                "category": "Income",
                "amount": 500.0,
                "balance_after": 1105.5,
            },
        ]

        elements = build_pdf_report_elements(report_rows, 5000.0, 1200.0, 3800.0)
        paragraph_text = [element.getPlainText() for element in elements if hasattr(element, "getPlainText")]
        tables = [element for element in elements if hasattr(element, "_cellvalues")]

        self.assertIn("ExpenseStats Report", paragraph_text)
        self.assertIn("Summary", paragraph_text)
        self.assertIn("Last 20 Transactions", paragraph_text)
        self.assertEqual(tables[0]._cellvalues[0], ["Total Income", "Rs 5000.00"])
        self.assertEqual(tables[0]._cellvalues[2], ["Final Balance", "Rs 3800.00"])
        self.assertEqual(
            tables[1]._cellvalues[0],
            ["Date", "Type", "Category", "Amount", "Balance After"],
        )
        self.assertEqual(
            tables[1]._cellvalues[1],
            ["2026-03-21", "Expense", "Food", "Rs 125.50", "Rs 980.00"],
        )

    def test_download_report_passes_last_20_rows_to_pdf_generator(self):
        user_id = self.create_user("alice")
        base_date = datetime(2026, 2, 1)

        for offset in range(25):
            transaction_type = "income" if offset % 2 == 0 else "expense"
            amount = float(50 + offset)
            category = "Salary" if transaction_type == "income" else "Bills"
            self.create_transaction(
                user_id,
                amount,
                transaction_type,
                category=category,
                when=base_date + timedelta(days=offset),
            )

        self.login("alice", "secret123")
        captured = {}
        report_fd, report_path = tempfile.mkstemp(suffix=".pdf")
        os.close(report_fd)

        def fake_generate_pdf_report(report_rows, total_income, total_expense, balance):
            captured["report_rows"] = report_rows
            captured["total_income"] = total_income
            captured["total_expense"] = total_expense
            captured["balance"] = balance
            with open(report_path, "wb") as pdf_file:
                pdf_file.write(b"%PDF-1.4\n%%EOF\n")
            return report_path

        try:
            with patch("app.generate_pdf_report", side_effect=fake_generate_pdf_report):
                response = self.client.get("/download_report", follow_redirects=False)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/pdf")
            self.assertEqual(len(captured["report_rows"]), 20)
            self.assertEqual(captured["report_rows"][0]["date"], "2026-02-25")
            self.assertEqual(captured["report_rows"][-1]["date"], "2026-02-06")
            self.assertEqual(captured["report_rows"][0]["balance_after"], captured["balance"])
            response.close()
        finally:
            if os.path.exists(report_path):
                os.unlink(report_path)

    def test_no_transaction_state_and_budget_validation(self):
        self.register()
        self.login()

        dashboard = self.client.get("/", follow_redirects=True)
        self.assertEqual(dashboard.status_code, 200)
        dashboard_text = dashboard.get_data(as_text=True)
        self.assertIn("No transactions yet", dashboard_text)
        self.assertIn("Future Projection", dashboard_text)
        self.assertIn("Spending Heatmap", dashboard_text)
        self.assertIn("No expense activity yet", dashboard_text)

        empty_chart = self.client.get("/chart", follow_redirects=True)
        self.assertEqual(empty_chart.status_code, 200)

        empty_report = self.client.get("/download_report", follow_redirects=False)
        self.assertEqual(empty_report.status_code, 200)
        self.assertEqual(empty_report.mimetype, "application/pdf")
        empty_report.close()

        empty_budget = self.client.post("/set_budget", data={"budget_amount": ""}, follow_redirects=True)
        self.assertIn("Budget amount is required", empty_budget.get_data(as_text=True))

        invalid_budget = self.client.post(
            "/set_budget",
            data={"budget_amount": "-100"},
            follow_redirects=True,
        )
        self.assertIn("Budget must be greater than 0", invalid_budget.get_data(as_text=True))

        valid_budget = self.client.post(
            "/set_budget",
            data={"budget_amount": "5000"},
            follow_redirects=True,
        )
        self.assertIn("Budget saved successfully", valid_budget.get_data(as_text=True))

        with app.app_context():
            budget = Budget.query.one()
            self.assertEqual(budget.amount, 5000)

    def test_about_page_requires_login_and_uses_new_branding(self):
        redirect_response = self.client.get("/about", follow_redirects=False)
        self.assertEqual(redirect_response.status_code, 302)
        self.assertTrue(redirect_response.headers["Location"].endswith("/login"))

        self.register()
        self.login()

        response = self.client.get("/about", follow_redirects=True)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("About ExpenseStats", page)
        self.assertIn("ExpenseStats", page)
        self.assertIn("Your spending, decoded.", page)

    def test_legacy_plain_text_password_is_upgraded_on_login(self):
        self.create_user("legacy-user", password="legacy-pass-1")

        with app.app_context():
            user = User.query.filter_by(username="legacy-user").first()
            user.password = "legacy-pass-1"
            db.session.commit()

        response = self.login("legacy-user", "legacy-pass-1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("upgraded to the secure format", response.get_data(as_text=True))

        with app.app_context():
            user = User.query.filter_by(username="legacy-user").first()
            self.assertNotEqual(user.password, "legacy-pass-1")
            self.assertTrue(check_password_hash(user.password, "legacy-pass-1"))


if __name__ == "__main__":
    unittest.main()
