# Copyright (c) BDist Development Team
# Distributed under the terms of the Modified BSD License.
from decimal import Decimal
from decimal import InvalidOperation
from logging.config import dictConfig
from os import getenv

from flask import Flask
from flask import abort
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from psycopg import Error
from psycopg.conninfo import make_conninfo
from psycopg.rows import namedtuple_row
from psycopg_pool import ConnectionPool

dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s:%(lineno)s - %(funcName)20s(): %(message)s",
            },
        },
        "handlers": {
            "wsgi": {
                "class": "logging.StreamHandler",
                "stream": "ext://flask.logging.wsgi_errors_stream",
                "formatter": "default",
            },
        },
        "root": {"level": "INFO", "handlers": ["wsgi"]},
    },
)

RATELIMIT_STORAGE_URI = getenv("RATELIMIT_STORAGE_URI", "memory://")

app = Flask(__name__)
app.config.from_prefixed_env()
log = app.logger
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=RATELIMIT_STORAGE_URI,
)


# Use the DATABASE_URL environment variable if it exists, otherwise use the default.
# Use the format postgres://username:password@hostname/database_name to connect to the database.
DATABASE_URL = make_conninfo(
    getenv("DATABASE_URL", "postgres://bank:bank@postgres/bank"),
    application_name=getenv("PGAPPNAME", "bank-web-app"),
)

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    kwargs={
        "autocommit": True,  # If True don't start transactions automatically.
        "row_factory": namedtuple_row,
    },
    min_size=4,
    max_size=10,
    open=True,
    check=ConnectionPool.check_connection,
    name="postgres_pool",
    timeout=5,
)


@app.route("/", methods=("GET",))
@app.route("/accounts", methods=("GET",))
@limiter.limit("1 per second")
def account_index():
    """Show all the accounts, most recent first."""

    with pool.connection() as conn, conn.cursor() as cur:
        accounts = cur.execute(
            """
                SELECT account_number, branch_name, balance
                FROM account
                ORDER BY account_number DESC;
                """,
            {},
        ).fetchall()
        log.debug("Found %s rows.", cur.rowcount)

    return render_template("account/index.html", accounts=accounts)


@app.route("/accounts/<account_number>/update", methods=("GET",))
@limiter.limit("1 per second")
def account_update_view(account_number):
    """Show the page to update the account balance."""

    with pool.connection() as conn, conn.cursor() as cur:
        account = cur.execute(
            """
                SELECT account_number, branch_name, balance
                FROM account
                WHERE account_number = %(account_number)s;
                """,
            {"account_number": account_number},
        ).fetchone()
        log.debug("Found %s rows.", cur.rowcount)

    # At the end of the `connection()` context, the transaction is committed
    # or rolled back, and the connection returned to the pool.

    if account is None:
        abort(404, description="Account not found.")

    return render_template("account/update.html", account=account)


@app.route("/accounts/<account_number>/update", methods=("POST",))
def account_update_save(account_number):
    """Update the account balance."""

    balance = request.form["balance"]

    if not balance:
        error = "Balance is required."
        abort(400, description=error)

    try:
        decimal_balance = Decimal(balance)
    except InvalidOperation:
        error = "Balance is required to be decimal."
        abort(400, description=error)

    if not decimal_balance.is_finite():
        error = "Balance must be a finite decimal."
        abort(400, description=error)

    if decimal_balance < 0:
        error = "Balance is required to be positive."
        abort(400, description=error)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE account
                SET balance = %(balance)s
                WHERE account_number = %(account_number)s;
                """,
            {"account_number": account_number, "balance": balance},
        )
        # The result of this statement is persisted immediately by the database
        # because the connection is in autocommit mode.
        log.debug("Updated %s rows.", cur.rowcount)

        if cur.rowcount == 0:
            abort(404, description="Account not found.")

    # The connection is returned to the pool at the end of the `connection()` context but,
    # because it is not in a transaction state, no COMMIT is executed.

    return redirect(url_for("account_index"))


@app.route("/accounts/<account_number>/delete", methods=("POST",))
def account_delete(account_number):
    """Delete the account."""

    with pool.connection() as conn, conn.cursor() as cur:
        try:
            with conn.transaction():
                # BEGIN is executed, a transaction started
                cur.execute(
                    """
                    DELETE FROM depositor
                    WHERE account_number = %(account_number)s;
                    """,
                    {"account_number": account_number},
                )
                cur.execute(
                    """
                    DELETE FROM account
                    WHERE account_number = %(account_number)s;
                    """,
                    {"account_number": account_number},
                )
                # These two operations run atomically in the same transaction
        except Error:
            log.exception("Failed to delete account %s.", account_number)
            abort(500)
        else:
            # COMMIT is executed at the end of the block.
            # The connection is in idle state again.
            log.debug("Deleted %s rows.", cur.rowcount)

            if cur.rowcount == 0:
                abort(404, description="Account not found.")

    # The connection is returned to the pool at the end of the `connection()` context

    return redirect(url_for("account_index"))


@app.route("/ping", methods=("GET",))
@limiter.exempt
def ping():
    log.debug("ping!")
    return jsonify({"message": "pong!", "status": "success"})


if __name__ == "__main__":
    app.run()
