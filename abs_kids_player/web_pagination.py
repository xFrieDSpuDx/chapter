"""Render numbered navigation for a page of browser results."""

from collections.abc import Callable
from html import escape


def page_numbers(current: int, last: int) -> list[int]:
    if last <= 7:
        return list(range(1, last + 1))
    start = max(1, min(current - 2, last - 4))
    return sorted({1, last, *range(start, start + 5)})


def pagination_link(url: str, label: str, text: str) -> str:
    return (
        f'<a class="pagination-control" data-library-link data-library-page '
        f'href="{escape(url)}" aria-label="{label}">{text}</a>'
    )


def pagination_arrow(
    target: int, last: int, url_for: Callable[[int], str], label: str, symbol: str,
) -> str:
    if 1 <= target <= last:
        return pagination_link(url_for(target), label, symbol)
    return f'<span class="pagination-control" aria-disabled="true" aria-label="{label}">{symbol}</span>'


def render_pagination(current: int, last: int, url_for: Callable[[int], str], summary: str) -> str:
    links = []
    previous = 0
    for number in page_numbers(current, last):
        if number > previous + 1:
            links.append('<span class="pagination-gap" aria-hidden="true">…</span>')
        if number == current:
            links.append(f'<span class="pagination-control" aria-current="page" aria-label="Page {number}">{number}</span>')
        else:
            links.append(pagination_link(url_for(number), f"Page {number}", str(number)))
        previous = number
    return (
        '<div class="pagination-footer">'
        f'<p class="muted">{escape(summary)}</p>'
        '<nav class="library-pagination" aria-label="Results pages">'
        f'{pagination_arrow(current - 1, last, url_for, "Previous page", "‹")}'
        f'<div class="pagination-numbers">{"".join(links)}</div>'
        f'{pagination_arrow(current + 1, last, url_for, "Next page", "›")}'
        '</nav></div>'
    )
