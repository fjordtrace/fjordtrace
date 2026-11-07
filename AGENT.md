# Research and abstention policy

1. **The organisation number is the identity anchor.** Every company starts from Brønnøysundregistrene.
   A brand, domain, handle or ad is only a candidate until it is tied back to that number.
2. **Exact-by-construction sources come first.** These are filed accounts and annual-account copies
   (fetched by org number), roles, subunits, group structure, NAV ads (employer org number = the company
   or one of its registered subunits), Wikidata (P2333 = org number), GLEIF LEI records (registeredAs = org
   number, Norwegian entity) and Støtteregisteret awards (recipient org number = this organisation).
3. **Website identity levels:**
   - `exact`: this org number is shown on the site and no other number appears as often.
   - `strong`: declared for this organisation (register, subunit, unshared e-mail domain, NAV, Wikidata) with
     its registered phone/e-mail/address or its legal name on the site and nothing pointing elsewhere; or a
     name-derived domain (spelling the whole legal name) that shows the full legal name with its company form
     while no other register entity has a name containing it, or the legal name plus a registered role holder.
   - `registry_declared`, `email_declared`, `source_declared`: the URL only, with no content attributed.
   - `ambiguous`: a shared domain, another org number, a different copyright entity, a foreign-group
     redirect, or a placeholder or parked page. The candidate is published with the reason, and nothing else.
   - Group members need `exact` proof before any content is attributed.
4. **What is published from a verified site:**
   - brand and description (labelled company-owned copy)
   - contact details and addresses found on the site
   - social handles that carry the company's name, brand or domain
   - news only with a genuine publication date that names the company
   - JobPosting only when the hiring organisation names the company
5. **Workforce** is the figure in the company section of the official notes ("Antall årsverk i
   regnskapsåret"). OCR readings must agree from two crops. Group figures are never published as the
   company's. A blank field is `not_available`, never zero.
6. **States are always explicit.** Every family carries available, not_available, blocked,
   not_applicable, ambiguous or failed, with a reason. Missing data is never converted to zero, and
   abstention is reported, not hidden.
7. **No LLM decides identity or invents a field.** The summary is deterministic and cites the claims it
   uses.
8. **Search providers** (optional, key-gated) only nominate domains, which must then pass the same
   org-number proof. Search results are never evidence and are not stored.
