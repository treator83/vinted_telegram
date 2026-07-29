def allow(listing, search):
    """
    Returns True if the listing passes all filters
    defined for the search.
    """

    # ----------------------------------
    # Keyword filter
    # ----------------------------------

    if search.keywords:

        text = f"{listing.title} {listing.subtitle}".lower()

        keywords = [
            keyword.lower()
            for keyword in search.keywords
        ]

        if not any(keyword in text for keyword in keywords):
            return False

    # ----------------------------------
    # Maximum price
    # ----------------------------------

    if listing.price_value > search.max_price:
        return False

    # ----------------------------------
    # Size filter
    # ----------------------------------

    if search.sizes:

        subtitle = listing.subtitle.lower()

        sizes = [
            str(size).lower()
            for size in search.sizes
        ]

        if not any(size in subtitle for size in sizes):
            return False

    # ----------------------------------
    # Condition filter
    # ----------------------------------

    if search.conditions:

        subtitle = listing.subtitle.lower()

        conditions = [
            condition.lower()
            for condition in search.conditions
        ]

        if not any(condition in subtitle for condition in conditions):
            return False

    return True