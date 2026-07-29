class Search:

    def __init__(
        self,
        id,
        name,
        url,
        max_price,
        keywords,
        sizes,
        conditions,
    ):

        self.id = id
        self.name = name
        self.url = url
        self.max_price = max_price
        self.keywords = keywords
        self.sizes = sizes
        self.conditions = conditions

    def __str__(self):

        return (
            f"{self.name} "
            f"(max £{self.max_price})"
        )