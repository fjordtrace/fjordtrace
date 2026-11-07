# Known limitations

- **Website recall is bounded without a search provider.** About 11% of companies list a website in the
  register. Registry e-mail domains, NAV, Wikidata and name-derived domains (.no, .com, the name without
  generic words) add more, but sites with no link to any of these are not found unless a search key is
  supplied. A name-derived domain counts only with this organisation number on the site, the full legal name
  with its company form when the name is unique in the register (needs the bulk snapshot), or the legal name
  plus a registered role holder.
- **JavaScript-only sites** are detected and reported, but not rendered (there is no browser dependency).
- **Robots.txt** is honoured. Sites that disallow all agents are reported as `blocked`.
- **Workforce:** companies that file a full annual report instead of Brønnøysund's standard notes
  (mostly large companies) have their figure only inside the attached report, which is not OCR'd. They
  are reported as `not_available`. The annual-account copy endpoint is rate limited (~60 starts/minute),
  so in a very large batch the remainder is reported as `blocked` by the time budget.
- **Annual-account copy hashes:** the register regenerates each PDF, changing only its trailer `/ID`. The
  recorded content hash is computed with that ID blanked, so it is reproducible across downloads.
- **NAV jobs:** a shipped index of active ads (a declared cache, rebuilt with `tools/build_nav_index.py`) is
  updated from the feed at run time, and every published ad is re-verified live. Without
  `SIGNALPOST_NAV_TOKEN`, NAV's rotating public token is used, so token failures are reported as `blocked`.
- **Reviews, ratings and sentiment are not collected.** No permitted source was available without
  restricted scraping.
- **Social metrics** (followers, posts) are not collected. Handles come only from the verified site or
  Wikidata.
- **Coverage of the extra official sources is naturally small:** LEI records (~11% of companies),
  Støtteregisteret awards (~15%), DiBK central approval (~3%), TED contract awards (~2%) and Mattilsynet
  inspections (~1%). Absence is reported as `not_available` with the source checked, never as a negative fact.
  At most 25 support awards, 15 TED notices and 10 inspections are published per company (most recent first).
