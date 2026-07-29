from search_manager import SearchManager


manager = SearchManager()

searches = manager.load()

print()

print(f"Loaded {len(searches)} searches")

print()

for search in searches:

    print(search)

    print(search.id)
    print(search.name)
    print(search.url)
    print(search.max_price)
    print(search.keywords)
    print(search.sizes)
    print(search.conditions)

    print("-" * 40)