from slug import slugify


def test_words_become_lowercase_and_hyphenated() -> None:
    assert slugify("Hello World") == "hello-world"
