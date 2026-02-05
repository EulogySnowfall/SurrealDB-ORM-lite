import re


def remove_quotes_for_variables(query: str) -> str:
    # Regex to remove single quotes around variables ($)
    return re.sub(r"'(\$[a-zA-Z_]\w*)'", r"\1", query)
