import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from werkzeug.security import check_password_hash

from finance_tracker.app import Budget, Transaction, User, app, db


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
            data={"username": username, "password": password},
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

        for path in ["/add", "/chart", "/download_report", "/edit/1", "/delete/1"]:
            route_response = self.client.get(path, follow_redirects=False)
            self.assertEqual(route_response.status_code, 302, path)
            self.assertTrue(route_response.headers["Location"].endswith("/login"), path)

        budget_response = self.client.post("/set_budget", data={}, follow_redirects=False)
        self.assertEqual(budget_response.status_code, 302)
        self.assertTrue(budget_response.headers["Location"].endswith("/login"))

    def test_register_hashes_password_and_login_logout_flow(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Registration successful", response.get_data(as_text=True))

        with app.app_context():
            user = User.query.filter_by(username="alice").first()
            self.assertIsNotNone(user)
            self.assertNotEqual(user.password, "secret123")
            self.assertTrue(check_password_hash(user.password, "secret123"))

        duplicate = self.register()
        self.assertIn("Username already exists", duplicate.get_data(as_text=True))

        bad_login = self.login(password="wrong-password")
        self.assertIn("Username or password is incorrect", bad_login.get_data(as_text=True))

        login_response = self.login()
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("Transactions", login_response.get_data(as_text=True))

        logout_response = self.client.get("/logout", follow_redirects=True)
        self.assertEqual(logout_response.status_code, 200)
        self.assertIn("Login", logout_response.get_data(as_text=True))

    def test_add_transaction_validation_and_success(self):
        self.register()
        self.login()

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

        dashboard = self.client.get("/", follow_redirects=True)
        page = dashboard.get_data(as_text=True)
        self.assertIn("&#8377;4000.00", page)
        self.assertIn("&#8377;1800.00", page)
        self.assertIn("monthly", page)
        self.assertNotIn("9999.00", page)
        self.assertIn("55.00%", page)
        self.assertIn("You spent most on Food.", page)

        filtered_day = self.client.get("/?filter_date=2026-03-20", follow_redirects=True)
        filtered_day_page = filtered_day.get_data(as_text=True)
        self.assertIn("Food", filtered_day_page)
        self.assertNotIn("Transport", filtered_day_page)
        self.assertIn("&#8377;1000.00", filtered_day_page)
        self.assertIn("&#8377;800.00", filtered_day_page)

        filtered_month = self.client.get("/?filter_month=2026-02", follow_redirects=True)
        filtered_month_page = filtered_month.get_data(as_text=True)
        self.assertIn("Bills", filtered_month_page)
        self.assertNotIn("Food", filtered_month_page)
        self.assertIn("&#8377;-300.00", filtered_month_page)

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

    def test_no_transaction_state_and_budget_validation(self):
        self.register()
        self.login()

        dashboard = self.client.get("/", follow_redirects=True)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("No transactions added yet", dashboard.get_data(as_text=True))

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


if __name__ == "__main__":
    unittest.main()
