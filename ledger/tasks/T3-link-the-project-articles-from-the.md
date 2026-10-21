# T3 Link the project articles from the README

- Status: done
- Lane: chore/blog-links-20261021
- Evidence: both article URLs return HTTP 200 (`curl -sI`); verify stamp committed

## Plan

Goal: the README links to the English and Finnish articles about the project on ollijunes.com, and the GitHub homepage points at the English article.

Checks: both links return HTTP 200; ruff and pytest unchanged.
