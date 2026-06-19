# Discourse scraper

`discourse_scrape.py` is a reusable engine for scraping **any** Discourse forum
(via its JSON API + sitemap). Output for each scraped site lives in its own
sibling folder, keyed by site name.

```
scrapers/discourse/
  discourse_scrape.py        # the engine
  threejs/                   # one scraped site (discourse.threejs.org)
    threejs_posts.jsonl      # every post (all topics)
    threejs_emails.jsonl     # only posts containing a contact email
```

## Run (defaults to the three.js forum)

```bash
python scrapers/discourse/discourse_scrape.py
# → writes scrapers/discourse/threejs/threejs_{posts,emails}.jsonl
```

The run is resumable: re-running skips topics already present in `--out`.

## Scrape a different Discourse site

Point `--base` at the forum and send the output to its own folder:

```bash
python scrapers/discourse/discourse_scrape.py \
  --base https://discourse.example.com \
  --out        scrapers/discourse/example/example_posts.jsonl \
  --emails-out scrapers/discourse/example/example_emails.jsonl
```

`threejs_emails.jsonl` is the file consumed by the data server (`server.py`).
