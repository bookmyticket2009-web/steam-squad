def assign_token(db, order_id, business_date):
    """Give the order the next SS-### token for `business_date`.

    MUST be called on a connection that already ran BEGIN IMMEDIATE (SQLite then holds
    the write lock until COMMIT, so two admins/requests can never read the same MAX).
    Tokens are never reused on the same day, even for cancelled orders.
    UNIQUE(business_date, token) in the schema is the final safety net.
    """
    row = db.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(token,4) AS INTEGER)),0) n FROM orders WHERE business_date=?",
        (business_date,),
    ).fetchone()
    token = f"SS-{int(row['n']) + 1:03d}"
    db.execute("UPDATE orders SET token=?, business_date=? WHERE id=?", (token, business_date, order_id))
    return token
