# Finance Tracker

A basic Flask finance tracker with:

- user registration and login
- transaction add, edit, and delete
- dashboard summary for income, expense, and balance
- backend-generated pie chart using `matplotlib`

This project is intended as a simple learning project, so authentication is intentionally basic and passwords are currently stored in plain text.

## Project Structure

```text
finance_tracker/
|-- app.py
|-- static/
`-- templates/
```

## Requirements

- Python 3.x
- Flask
- Flask-SQLAlchemy
- matplotlib

## Installation

```bash
pip install -r requirements.txt
```

## Run the App

From the project root:

```bash
cd finance_tracker
python app.py
```

Open:

```text
http://127.0.0.1:5000/
```

## Create the Database

Run this once before using the app:

```bash
cd finance_tracker
python
```

Then in the Python shell:

```python
from app import app, db
with app.app_context():
    db.create_all()
```

## Main Routes

- `/register` - create a user account
- `/login` - login page
- `/logout` - logout
- `/` - dashboard
- `/add` - add transaction
- `/edit/<id>` - edit transaction
- `/delete/<id>` - delete transaction
- `/chart` - view income vs expense pie chart

## Notes

- SQLite database file is created automatically by SQLAlchemy
- pie chart image is generated in `finance_tracker/static/chart.png`
- this project is for learning/demo purposes
