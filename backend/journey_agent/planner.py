"""Semantic query layer: question -> QueryPlan -> parameterised SQL.

Why a plan instead of letting the model write SQL?
  A 7B model writing free SQL is where the wrong numbers came from ("Meesho,
  35 search events" was really 35 *journeys* / 70 searches). A plan only has a
  handful of fields - metric, group_by, filters, order, limit - and the SQL for
  it is generated here, with the counting rules (DISTINCT journeys after a
  join, app-attributed searches, ...) encoded once and tested.

Parsing is two-tier:
  1. ``RuleParser`` - deterministic and instant. It accepts a question only if
     every word is understood (a coverage check). That precision guard is what
     stops "all app name of this user" being mistaken for "all apps".
  2. ``LLMPlanner`` - the model fills the same plan schema (constrained JSON)
     for anything the rules do not fully cover. Its output is validated before
     use. Questions no plan can express fall through to the SQL agent.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date

from .context import AppMatcher, Resolution
from .store import escape_like

METRICS = ("journeys", "searches", "users", "apps", "avg_searches",
           "avg_duration", "total_duration", "purchase_rate")
GROUPS = ("app", "user", "outcome", "status", "date", "hour", "query", "apps_per_journey",
          "start_app")
INTENTS = ("aggregate", "list_journeys", "user_detail", "journey_detail", "similar", "quality",
           "start_funnel",
           "summary", "unsupported", "out_of_scope", "smalltalk", "help", "agent")

UNSUPPORTED_FIELDS = (
    "price, order id, order value, product, brand, payment, revenue, clicks, cart, "
    "location, device, age or gender")


@dataclass
class QueryPlan:
    intent: str | None = None
    metric: str | None = None
    group_by: str | None = None
    user_ids: list[int] = field(default_factory=list)
    journey_ids: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    status: str | None = None            # open | closed
    outcome: str | None = None           # purchased | timeout
    multi_app: bool | None = None        # True: >1 app, False: exactly one app
    date_from: str | None = None
    date_to: str | None = None
    query_text: str | None = None
    order: str = "desc"
    limit: int | None = None
    singular: bool = False               # "which app" (leader) vs "which apps" (list)
    share: bool = False                  # show each group's share of the filtered total
    focus_apps: list[str] = field(default_factory=list)   # "Amazon's share ... apps ahead of us"
    start_apps: list[str] = field(default_factory=list)   # journeys whose FIRST search was here
    reason: str | None = None            # for unsupported
    source: str = "rules"
    confidence: float = 1.0
    unknown_words: list[str] = field(default_factory=list)

    def has_filters(self) -> bool:
        return bool(self.user_ids or self.journey_ids or self.apps or self.status or
                    self.start_apps or
                    self.outcome or self.multi_app is not None or self.date_from or
                    self.date_to or self.query_text)

    def to_dict(self) -> dict:
        data = asdict(self)
        return {k: v for k, v in data.items() if v not in (None, [], False) or k == "intent"}

    def describe(self) -> str:
        """Human-readable restatement, shown as the 'resolved question'."""
        if self.intent == "quality":
            return "journey data-quality report"
        if self.intent == "user_detail" and self.user_ids:
            return f"full profile of user {self.user_ids[0]}"
        if self.intent == "journey_detail" and self.journey_ids:
            return f"details of journey {self.journey_ids[0]}"
        if self.intent == "start_funnel" and self.start_apps:
            return f"what happened to journeys that started on {' / '.join(self.start_apps)}"
        if self.intent == "similar" and self.journey_ids:
            return f"journeys similar to {self.journey_ids[0]}"
        parts = []
        metric = {"avg_searches": "average searches per journey",
                  "avg_duration": "average journey duration",
                  "total_duration": "total journey duration",
                  "purchase_rate": "purchase rate", None: "journeys"}.get(self.metric, self.metric)
        if self.intent == "list_journeys":
            parts.append("list journeys")
        else:
            parts.append(metric)
        if self.group_by:
            parts.append(f"by {self.group_by.replace('_', ' ')}")
        parts.extend(describe_filters(self))
        return " ".join(parts)


def describe_filters(plan: QueryPlan) -> list[str]:
    parts = []
    if plan.user_ids:
        parts.append("for user " + ", ".join(map(str, plan.user_ids)))
    if plan.start_apps:
        parts.append("that started on " + " / ".join(plan.start_apps))
    if plan.journey_ids:
        parts.append("for journey " + ", ".join(plan.journey_ids))
    if plan.apps:
        parts.append("on " + " / ".join(plan.apps))
    if plan.outcome:
        parts.append({"purchased": "that ended in a purchase",
                      "timeout": "that timed out"}[plan.outcome])
    elif plan.status:
        parts.append(f"that are {plan.status}")
    if plan.multi_app is True:
        parts.append("that used more than one app")
    elif plan.multi_app is False:
        parts.append("that used a single app")
    if plan.query_text:
        parts.append(f"matching '{plan.query_text}'")
    if plan.date_from and plan.date_from == plan.date_to:
        parts.append(f"on {plan.date_from}")
    else:
        if plan.date_from:
            parts.append(f"from {plan.date_from}")
        if plan.date_to:
            parts.append(f"to {plan.date_to}")
    return parts


# --------------------------------------------------------------------------- #
# Lexicon
# --------------------------------------------------------------------------- #

def _words(*groups: str) -> set[str]:
    return {w for g in groups for w in g.split()}


W_JOURNEY = _words("journey journeys hunt hunts session sessions")
W_SEARCH = _words("search searches searched searching event events query queries "
                  "keyword keywords term terms")
W_USER = _words("user users people person persons customer customers shopper shoppers")
W_APP = _words("app apps application applications platform platforms store stores")
W_COUNT = _words("how many count number total no counts much")
W_RANK_DESC = _words("most top highest max maximum biggest largest popular leading best "
                     "heaviest frequent more")
W_RANK_ASC = _words("least fewest lowest min minimum smallest bottom less rarely")
W_WHICH = _words("which what who whose")
W_LIST = _words("list all every each show give display name names per by wise breakdown "
                "distribution split across between vs versus compare comparison")
W_AVG = _words("average avg mean typical typically")
W_DURATION = _words("duration long time spend spends spent spending minutes seconds hours")
W_RATE = _words("rate rates conversion convert converted converts percentage percent "
                "ratio share")
W_DETAIL = _words("detail details profile info information everything history activity "
                  "complete full entire about summary record records overview data "
                  "journey-level all")
W_DATE = _words("day days daily date dates trend trends over timeline")
W_HOUR = _words("hour hours hourly")
W_STATUS = _words("status statuses outcome outcomes result results state")
W_PURCHASE = _words("purchased purchase purchases bought buy buying converted conversion "
                    "ordered")
W_TIMEOUT = _words("timeout timeouts timed out time-out abandoned abandon dropped expired "
                   "dropoff drop-off")
W_OPEN = _words("open ongoing unfinished pending unclosed progress")
W_CLOSED = _words("closed ended finished completed")
W_MULTI = _words("cross multi multiple several many")
W_SINGLE = _words("single only one")
W_SIMILAR = _words("similar like related resembling closest nearest same-as")
W_SUMMARY = _words("summary overview dataset stats statistics totals file")
W_UNSUPPORTED = _words("price prices cost costs revenue revenues gmv payment payments paid "
                       "amount amounts value values cart clicks click clicked location city "
                       "cities state-wise device devices gender age brand brands spend spent "
                       "sales pay pays paying spend spending bill bills discount discounts "
                       "coupon coupons rating ratings review reviews delivery refund refunds "
                       "profit profits margin inventory stock salary income")
FILLER = _words(
    "a an the of for in on at to from with and or is are was were be been do does did "
    "has have had me my i we you your us our please pls plz kindly ok okay okk so now "
    "then also too just only can could would will tell get find see check give let "
    "want need know show list this that these those it its their them there here "
    "any some which what who how many much used use using uses made make did done "
    "ones one than more as per by each got get sir bro hi hey hello thanks thank "
    "particular specific same selected above previous said mentioned again recheck "
    "once more other another up down into out whole his her he she him hers guy "
    "id ids no number name names wise under over within between where when while "
    "all every complete full entire latest recent recently first second third last top "
    "vs versus versus? yes okay? ok? like")
REFERENCE_WORDS = _words("this that same particular specific selected above previous said "
                         "mentioned his her he she him hers guy")
_ALL_VOCAB = (W_JOURNEY | W_SEARCH | W_USER | W_APP | W_COUNT | W_RANK_DESC | W_RANK_ASC |
              W_WHICH | W_LIST | W_AVG | W_DURATION | W_RATE | W_DETAIL | W_DATE | W_HOUR |
              W_STATUS | W_PURCHASE | W_TIMEOUT | W_OPEN | W_CLOSED | W_MULTI | W_SINGLE |
              W_SIMILAR | W_SUMMARY | W_UNSUPPORTED | FILLER |
              _words("ended end ending ends lead leads led result resulted went go"
                     " share contribution portion ahead above behind beat beating overall right"
                     " start starts started starting begin begins began originate originated"
                     " else elsewhere somewhere other others leave leaves left leaving switch"
                     " switched switches switching move moved moves up buying stay stayed stays"
                     " versus instead end"
                     " currently current we us our ours"
                     " used spread across both apps? mostly frequently often"
                     " till until before after since"))

_DETAIL_HEADS = _words("detail details profile everything history info information activity summary")
_FRAGMENT_START = re.compile(r"^\s*(and|&|also|what about|how about|same (for|with)|now|then|"
                             r"only|just|for|of|on|in|but|plus|same|ok(ay)? (and|now|for|of))\b", re.I)
SMALLTALK = re.compile(r"^\s*(hi+|hello+|hey+|thanks?( you)?|thank u|ok(ay)?|cool|great|"
                       r"good (morning|evening|afternoon)|bye|nice)\s*[!.?]*\s*$", re.I)
HELP = re.compile(r"\b(what can you (do|answer)|help|how (do|to) (i )?use|examples?)\b", re.I)
QUALITY = re.compile(r"\b(data[\s-]?quality|quality report|load(?:ing)? warnings?|skipped records?|"
                     r"duplicate records?|inconsistent records?)\b", re.I)

_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_ISO_DATE = re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")
_DM_DATE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|"
                      r"oct|nov|dec)[a-z]*\.?(?:\s+(20\d\d))?\b", re.I)
_MD_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
                      r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?\b", re.I)
_TOP_N = re.compile(r"\b(?:top|first|best|bottom|last|least|lowest)\s+(\d{1,4})\b"
                    r"|\b(\d{1,4})\s+(?:most|top|least|biggest|largest|highest|lowest)\b", re.I)
_WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10}
_QUOTED = re.compile(r"(?<![A-Za-z])[\"'“‘]([^\"'”’]{2,80})[\"'”’](?![A-Za-z])")
_QUERY_TRIGGER = re.compile(
    r"\b(?:search(?:ed|es|ing)?\s+(?:for|about)|looking\s+for|looked\s+for|"
    r"(?:journeys?|searches|queries|users?|people)\s+(?:about|for|containing|mentioning|"
    r"with\s+(?:query|keyword|term))|containing|keyword|query\s+text|who\s+searched"
    r"(?:\s+for)?)\s+(.+?)\s*[?.!]*$", re.I)


def _tokenize(text: str) -> list[str]:
    text = re.sub(r"['’]s\b", "", text.lower())          # "what's", "Amazon's"
    return re.findall(r"[a-z]+(?:-[a-z]+)?|\d+|%", text.lower())


def _fuzzy_known(word: str) -> str | None:
    """Map a typo ('journies', 'serches', 'purchse') to a known lexicon word."""
    if len(word) < 5:
        return None
    try:
        from rapidfuzz import process, fuzz
    except ImportError:
        return None
    hit = process.extractOne(word, _FUZZY_TARGETS, scorer=fuzz.ratio, score_cutoff=85)
    return hit[0] if hit else None


_FUZZY_TARGETS = sorted(w for w in (_ALL_VOCAB - FILLER) if len(w) >= 5)


# --------------------------------------------------------------------------- #
# Rule parser
# --------------------------------------------------------------------------- #

class RuleParser:
    def __init__(self, apps: AppMatcher, data_year: int | None = None):
        self.apps = apps
        self.data_year = data_year or date.today().year

    # -------------------------------------------------------------- dates
    def _dates(self, text: str) -> tuple[list[str], str]:
        found: list[tuple[int, str]] = []

        def iso(y, m, d):
            try:
                return date(int(y), int(m), int(d)).isoformat()
            except ValueError:
                return None

        for rx, order in ((_ISO_DATE, "ymd"), (_DM_DATE, "dmy"), (_MD_DATE, "mdy")):
            for match in rx.finditer(text):
                g = match.groups()
                if order == "ymd":
                    value = iso(*g)
                elif order == "dmy":
                    value = iso(g[2] or self.data_year, _MONTHS[g[1][:3].lower()], g[0])
                else:
                    value = iso(g[2] or self.data_year, _MONTHS[g[0][:3].lower()], g[1])
                if value:
                    found.append((match.start(), value))
                text = text[:match.start()] + " " * (match.end() - match.start()) + text[match.end():]
        return [v for _, v in sorted(found)], text

    # -------------------------------------------------------------- parse
    def parse(self, question: str, res: Resolution) -> QueryPlan:
        plan = QueryPlan(user_ids=list(res.user_ids), journey_ids=list(res.journey_ids),
                         apps=list(res.apps))
        text = question.strip()

        if SMALLTALK.match(text):
            plan.intent = "smalltalk"
            return plan
        if QUALITY.search(text):
            plan.intent = "quality"
            return plan

        # "journeys that start on Amazon": the app is where the journey began.
        for app, (start, end) in self.apps.find(text):
            before = text[max(0, start - 40):start].lower()
            if re.search(r"(start\w*|begin\w*|began|first\s+(?:app|search\w*|on)|originat\w*)"
                         r"(?:\s+(?:on|in|with|from|at|is|was))?\s*$", before):
                plan.start_apps.append(app)
                if app in plan.apps:
                    plan.apps.remove(app)

        # Remove entity mentions so they do not count as unknown words.
        masked = text
        for start, end in self.apps.spans(masked):
            masked = masked[:start] + " " * (end - start) + masked[end:]
        masked = re.sub(r"\buser(?:[\s_]*(?:assoc(?:iate)?[\s_]*)?id)?\s*(?:no\.?|number|#|:|=)?\s*\d+",
                        " ", masked, flags=re.I)
        masked = re.sub(r"\busers?\s*(?:ids?)?\s*:?\s*\d+(?:\s*(?:,|and|&|or|vs)\s*\d+)+",
                        " ", masked, flags=re.I)
        masked = re.sub(r"\bj\s?-?\d{3,}\b", " ", masked, flags=re.I)
        dates, masked = self._dates(masked)
        if dates:
            plan.date_from, plan.date_to = dates[0], dates[-1]
            low = masked.lower()
            if len(dates) == 1 and re.search(r"\b(after|since|from)\b", low):
                plan.date_to = None
            elif len(dates) == 1 and re.search(r"\b(before|until|till|upto|up to)\b", low):
                plan.date_from = None

        quoted = _QUOTED.search(masked)
        trigger = _QUERY_TRIGGER.search(masked)
        if quoted:
            plan.query_text = quoted.group(1).strip()
            masked = masked.replace(quoted.group(0), " ")
        elif trigger:
            candidate = trigger.group(1).strip()
            words = _tokenize(candidate)
            # Only treat the tail as a search phrase if it is not our own vocabulary.
            if words and not all(w in _ALL_VOCAB for w in words):
                plan.query_text = candidate
                masked = masked[:trigger.start(1)] + " " * len(candidate) + masked[trigger.end(1):]

        masked = re.sub(r"\b(top|first|best|bottom|last|least|lowest)\s+(two|three|four|five|six|seven|eight|nine|ten)\b",
                        lambda m: f"{m.group(1)} {_WORD_NUM[m.group(2).lower()]}", masked, flags=re.I)
        limit_match = _TOP_N.search(masked)
        if limit_match:
            plan.limit = int(limit_match.group(1) or limit_match.group(2))
            masked = masked[:limit_match.start()] + " top " + masked[limit_match.end():]

        tokens = []
        unknown = []
        for tok in _tokenize(masked):
            if tok.isdigit():
                continue                              # stray numbers are harmless
            if tok in _ALL_VOCAB:
                tokens.append(tok)
                continue
            fixed = _fuzzy_known(tok)
            if fixed:
                tokens.append(fixed)
            else:
                unknown.append(tok)
        plan.unknown_words = unknown
        words = set(tokens)
        low = " " + " ".join(tokens) + " "

        # ---- cues -------------------------------------------------------
        has_journey = bool(words & W_JOURNEY)
        has_search = bool(words & W_SEARCH)
        has_user = bool(words & W_USER)
        has_app = bool(words & W_APP) or bool(plan.apps)
        count_cue = bool(re.search(r" how (many|much) | count | number of | total | no of ", low))
        rank_desc = bool(words & W_RANK_DESC) or bool(plan.limit)
        rank_asc = bool(words & W_RANK_ASC)
        which_cue = bool(words & {"which", "who", "whose"}) or bool(
            re.search(r" what (app|apps|user|users|query|queries|day|days|hour|status) ", low))
        list_cue = bool(words & {"list", "all", "every", "each", "per", "by", "wise",
                                 "breakdown", "distribution", "split", "name", "names",
                                 "compare", "comparison", "vs", "versus"})
        avg_cue = bool(words & W_AVG)
        duration_cue = bool(words & {"duration", "long", "spent", "spend", "spends", "spending"}) \
            or bool(re.search(r" (?:spend|spends|spent|spending) (?:the )?(?:most |least )?time ", low))
        rate_cue = bool(words & W_RATE)
        # "purchase share" / "share of purchases" = each app's slice of ALL purchases,
        # not the app's own conversion rate.
        share_cue = bool(words & {"share", "contribution", "portion"}) and bool(
            words & (W_PURCHASE | W_TIMEOUT | W_OPEN | W_JOURNEY | W_SEARCH)) and not (
            words & {"rate", "rates", "conversion", "convert", "converts"})
        if share_cue:
            rate_cue = False
        detail_cue = bool(words & W_DETAIL)
        similar_cue = bool(words & W_SIMILAR) and bool(plan.journey_ids)
        summary_cue = bool(words & W_SUMMARY) and not (has_user and plan.user_ids)

        # ---- filters from words ------------------------------------------
        if words & W_PURCHASE and not rate_cue:
            plan.outcome = "purchased"
        if (words & W_TIMEOUT) or re.search(r" timed out | time out ", low):
            plan.outcome = "timeout" if not plan.outcome else plan.outcome
        if words & W_OPEN and not plan.outcome:
            plan.status = "open"
        if words & W_CLOSED and not plan.outcome:
            plan.status = "closed"
        if re.search(r" (cross|multi|multiple|several) (app|apps|platform|platforms) |"
                     r" more than (one|1) (app|apps) | cross | multi ", low):
            plan.multi_app = True
        elif re.search(r" (single|one|1) (app|platform) | only one (app|platform) ", low):
            plan.multi_app = False

        if plan.multi_app is not None:
            # "cross app journeys": the word "app" is part of a filter, not a dimension.
            low = re.sub(r" (?:cross|multi|multiple|several|single|one|1|only one) (?:app|apps|platform|platforms) |"
                         r" more than (?:one|1) (?:app|apps) ", " ", low)
            tokens = low.split()
            words = set(tokens)
            has_app = bool(plan.apps) or bool(words & W_APP)

        fragment = bool(_FRAGMENT_START.match(text)) and not (
            count_cue or rank_desc or rank_asc or avg_cue or rate_cue or
            (words & (W_JOURNEY | W_SEARCH | W_APP | W_STATUS | _DETAIL_HEADS)))

        # ---- intent -------------------------------------------------------
        unsupported_words = words & W_UNSUPPORTED
        if duration_cue:
            unsupported_words -= {"spend", "spent", "spending"}
        if unsupported_words and not (words & {"value"} and rate_cue):
            plan.intent = "unsupported"
            hit = sorted(unsupported_words)
            plan.reason = ", ".join(hit)
            return self._finish(plan)
        if HELP.search(text) and not (has_journey or has_search or has_app or has_user):
            plan.intent = "help"
            return plan
        if similar_cue:
            plan.intent = "similar"
            return self._finish(plan)

        analytic = count_cue or rank_desc or rank_asc or avg_cue or rate_cue or duration_cue

        if plan.journey_ids and not analytic and not (has_app and list_cue):
            plan.intent = "journey_detail"
            return self._finish(plan)

        group = self._group_by(tokens, low, has_app, has_user, which_cue, list_cue,
                               rank_desc or rank_asc, count_cue, plan)

        if fragment:
            plan.intent = None                        # merged with the previous question
            return self._finish(plan)

        if (len(plan.user_ids) == 1 and not analytic and group is None
                and not (plan.apps or plan.outcome or plan.status or plan.query_text
                         or plan.date_from or plan.date_to or plan.multi_app is not None)):
            plan.intent = "user_detail"
            return self._finish(plan)

        if summary_cue and not (analytic or group or plan.has_filters()):
            plan.intent = "summary"
            return self._finish(plan)

        metric = self._metric(tokens, low, count_cue, avg_cue, rate_cue, duration_cue,
                              has_journey, has_search, has_user, has_app, group)

        if plan.start_apps and group is None and not (avg_cue or duration_cue or rate_cue):
            plan.intent = "start_funnel"
            # The breakdown already splits every outcome, so "end up buying" is the
            # measure here, not a filter - unless the user says "only purchased".
            if not re.search(r" only ", low):
                plan.outcome = plan.status = None
            return self._finish(plan)

        if (group is None and has_journey and (rank_desc or rank_asc) and not count_cue
                and metric == "searches" and not avg_cue):
            plan.intent = "list_journeys"             # "journeys with the most searches"
            plan.order = "asc" if rank_asc and not (words & (W_RANK_DESC - {"top", "more"})) else "desc"
            plan.limit = plan.limit or 10
            return self._finish(plan)

        wants_rows = (has_journey and not analytic and group is None and
                      (plan.has_filters() or list_cue or which_cue or detail_cue))
        if plan.query_text and not analytic and group is None:
            wants_rows = True
        if wants_rows:
            plan.intent = "list_journeys"
            return self._finish(plan)

        if share_cue:
            plan.intent = "aggregate"
            plan.share = True
            # "who are the apps ahead of us": "who" does not mean users when apps are named.
            if group in (None, "user") and (words & W_APP or plan.apps) and \
                    not (words & {"user", "users", "customer", "customers", "people", "shopper"}):
                group = "app"
            plan.group_by = group or "app"
            if plan.group_by == "app" and plan.apps:
                plan.focus_apps, plan.apps = plan.apps, []     # rank against every app
            plan.metric = "searches" if (has_search and not (words & W_PURCHASE)) else "journeys"
            if " ahead " in low or " above " in low or " beat" in low:
                plan.limit = None
            return self._finish(plan)

        if group or analytic or metric:
            plan.intent = "aggregate"
            plan.group_by = group
            plan.metric = metric or ("searches" if group == "app" else "journeys")
            plan.order = "asc" if rank_asc and not (words & (W_RANK_DESC - {"top", "more"})) else "desc"
            plan.singular = self._is_singular(tokens, group)
            if plan.group_by is None and plan.metric in ("users", "apps") and has_user and \
                    plan.user_ids and plan.metric == "users":
                plan.metric = "journeys"
            return self._finish(plan)

        if plan.has_filters() and (has_journey or plan.outcome or plan.status):
            plan.intent = "list_journeys"
            return self._finish(plan)

        # No intent word at all: a fragment ("of particular user", "and Flipkart?").
        plan.intent = None
        return self._finish(plan)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _group_by(tokens, low, has_app, has_user, which_cue, list_cue, rank, count_cue, plan):
        """Pick the dimension the question is broken down by."""
        if re.search(r" (?:start|starting|started|first|entry|origin) (?:app|apps|platform|platforms) ", low):
            return "start_app"
        # Explicit "per X" / "by X" / "each X" / "X wise".
        explicit = re.search(r" (?:per|by|each|every|across|wise) (app|apps|platform|platforms|"
                             r"user|users|customer|customers|people|day|days|date|dates|hour|"
                             r"hours|status|outcome|outcomes|query|queries|search|searches) ", low)
        if explicit:
            return _DIM_OF[explicit.group(1)]
        wise = re.search(r" (app|user|day|date|hour|status|outcome|query) wise ", low)
        if wise:
            return _DIM_OF[wise.group(1)]
        if re.search(r" (daily|trend|trends|timeline|over time) ", low):
            return "date"
        if " hourly " in low or re.search(r" (which|what) (hour|time of day) ", low):
            return "hour"
        if re.search(r" (status|statuses|outcome|outcomes|results) ", low) and (list_cue or count_cue or which_cue):
            return "outcome"

        # "how many journeys per ..." handled above; for "which/top/most/all X",
        # the dimension is the X next to the cue that is not the counted metric.
        counted = None
        m = re.search(r" (?:how many|number of|count of|total) (\w+) ", low)
        if m:
            counted = m.group(1)
        dims = []
        journey_noun = any(t in W_JOURNEY for t in tokens)
        for tok in tokens:
            dim = _DIM_OF.get(tok)
            if dim == "query" and tok in ("search", "searches") and (
                    journey_noun or " most searched " not in low and " top searches " not in low):
                dim = None
            if dim and dim not in dims and not (counted and tok == counted):
                dims.append(dim)
        # When the user asks "which X", X is the group.
        which = re.search(r" (?:which|what|top|most|least|all|every|list|name|names|who|whose)"
                          r"(?: \w+)?? (app|apps|platform|platforms|application|applications|"
                          r"user|users|customer|customers|people|query|queries|searches|search|"
                          r"day|days|date|dates) ", low)
        if which and (which_cue or list_cue or rank):
            dim = _DIM_OF[which.group(1)]
            if dim == "query" and which.group(1) in ("search", "searches") and (
                    not rank or journey_noun):
                dim = None
            if dim:
                return dim
        if " who " in low and (rank or which_cue):
            return "user"
        # "app name" / "apps of user 5330" / "apps used"
        if has_app and (list_cue or which_cue or " used " in low or rank or
                        re.search(r" (app|apps) ", low)) and "app" in dims:
            if not (count_cue and counted in ("app", "apps", "platform", "platforms")):
                return "app"
        if rank and dims:
            candidates = [d for d in dims if d not in ("query",) or " most searched " in low]
            for d in candidates:
                if d != "journeys":
                    return d
        return None

    @staticmethod
    def _metric(tokens, low, count_cue, avg_cue, rate_cue, duration_cue,
                has_journey, has_search, has_user, has_app, group):
        if rate_cue and (" purchase" in low or " conversion " in low or " convert" in low or
                         " rate " in low):
            return "purchase_rate"
        if avg_cue and duration_cue:
            return "avg_duration"
        if duration_cue and re.search(r" (?:spend|spends|spent|spending) "
                                      r"(?:the )?(?:most |least )?time ", low):
            return "total_duration"
        if duration_cue and re.search(r" (how long|duration|time spent) ", low):
            return "avg_duration"
        if avg_cue and (has_search or " searches per " in low):
            return "avg_searches"
        m = re.search(r" (?:how many|number of|count of|total|no of) (\w+)", low)
        if m:
            noun = m.group(1)
            if noun in W_SEARCH:
                return "searches"
            if noun in W_USER:
                return "users" if group != "user" else "journeys"
            if noun in W_APP:
                return "apps" if group != "app" else "searches"
            if noun in W_JOURNEY:
                return "journeys"
        # "most searches", "most journeys", "most apps", "most used"
        m = re.search(r" (?:most|highest|least|fewest|lowest|max|maximum|more|top) (\w+)", low)
        if m:
            noun = m.group(1)
            if noun in W_SEARCH or noun in ("used", "use", "popular", "frequent", "frequently"):
                return "searches"
            if noun in W_JOURNEY or noun in W_PURCHASE or noun in W_TIMEOUT:
                return "journeys"          # "most purchases" = most purchased journeys
            if noun in W_APP and group != "app":
                return "apps"
            if noun in W_USER and group != "user":
                return "users"
        if has_search and not has_journey:
            return "searches"
        if has_journey and not has_search:
            return "journeys"
        if avg_cue:
            return "avg_searches"
        return None

    @staticmethod
    def _is_singular(tokens, group) -> bool:
        """'which app has the most X' -> one winner; 'which apps', 'all apps', 'per app' -> a list."""
        singular = {"app": {"app", "platform", "application", "store"},
                    "user": {"user", "customer", "person", "shopper"},
                    "query": {"query", "search"}, "date": {"day", "date"}, "hour": {"hour"}}
        plural_nouns = {"app": {"apps", "platforms", "applications", "stores"},
                        "user": {"users", "customers", "people", "shoppers"},
                        "query": {"queries"}, "date": {"days", "dates"}, "hour": {"hours"}}
        list_words = {"all", "list", "every", "each", "names", "per", "by", "wise", "breakdown",
                      "distribution", "split", "compare", "comparison", "vs", "versus",
                      "daily", "hourly", "ranking", "rank"}
        if not group or group not in singular:
            return False
        words = set(tokens)
        if words & plural_nouns[group] or words & list_words:
            return False
        return bool(words & singular[group])

    def _finish(self, plan: QueryPlan) -> QueryPlan:
        # Only unknown words that look like real content lower the confidence.
        plan.confidence = 1.0 if not plan.unknown_words else 0.4
        return plan


_DIM_OF = {
    "app": "app", "apps": "app", "platform": "app", "platforms": "app",
    "application": "app", "applications": "app", "store": "app", "stores": "app",
    "user": "user", "users": "user", "customer": "user", "customers": "user",
    "people": "user", "person": "user", "shopper": "user", "shoppers": "user",
    "day": "date", "days": "date", "date": "date", "dates": "date",
    "hour": "hour", "hours": "hour", "status": "outcome", "outcome": "outcome",
    "outcomes": "outcome", "query": "query", "queries": "query",
    "search": "query", "searches": "query",
}


def merge_follow_up(base: QueryPlan, delta: QueryPlan) -> QueryPlan:
    """Apply a fragment ("and for Flipkart?", "only purchased") to the previous plan."""
    merged = replace(base, source=base.source, unknown_words=[], confidence=base.confidence)
    if delta.user_ids:
        merged.user_ids = delta.user_ids
    if delta.journey_ids:
        merged.journey_ids = delta.journey_ids
    if delta.apps:
        merged.apps = delta.apps
    if delta.start_apps:
        merged.start_apps = delta.start_apps
    for attr in ("status", "outcome", "multi_app", "date_from", "date_to", "query_text", "limit"):
        value = getattr(delta, attr)
        if value is not None:
            setattr(merged, attr, value)
    # A user detail question that gains filters becomes a filtered journey list.
    if merged.intent == "user_detail" and (merged.apps or merged.outcome or merged.status
                                           or merged.query_text):
        merged.intent = "list_journeys"
    return merged


# --------------------------------------------------------------------------- #
# LLM planner (constrained JSON) - used when the rules are not confident
# --------------------------------------------------------------------------- #

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "metric": {"type": "string", "enum": list(METRICS) + ["none"]},
        "group_by": {"type": "string", "enum": list(GROUPS) + ["none"]},
        "user_ids": {"type": "array", "items": {"type": "integer"}},
        "journey_ids": {"type": "array", "items": {"type": "string"}},
        "apps": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["open", "closed", "any"]},
        "outcome": {"type": "string", "enum": ["purchased", "timeout", "any"]},
        "multi_app": {"type": "string", "enum": ["yes", "no", "any"]},
        "date_from": {"type": "string"},
        "date_to": {"type": "string"},
        "query_text": {"type": "string"},
        "order": {"type": "string", "enum": ["desc", "asc"]},
        "limit": {"type": "integer"},
        "share": {"type": "boolean"},
        "focus_apps": {"type": "array", "items": {"type": "string"}},
        "start_apps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent"],
}

PLANNER_SYSTEM = """You convert a question about a shopping-journey dataset into a JSON query plan.
You never answer the question and never compute numbers.

Dataset: journeys (grouped product hunts) with user id, apps used and searches per app,
search phrase, first/last search time, status (open/closed) and close reason
(purchased/timeout). Apps (exact names): {apps}. Dates: {first_date} to {last_date}.
It has NO price, order, product, brand, payment, revenue, clicks, location or demographics.

Fields:
- intent: aggregate (counts/rankings/breakdowns) | list_journeys (show matching journeys)
  | user_detail (everything about ONE user) | journey_detail (one journey id)
  | similar (journeys similar to a journey id) | summary (dataset overview)
  | start_funnel (what happened to journeys that STARTED on an app: stayed, moved to
  other apps, bought)
  | unsupported (asks for a business field the dataset does not contain)
  | out_of_scope (asks for code, project/source files, creative content, or unrelated
  general knowledge) | agent (needs custom SQL:
  comparisons of two different filters, ratios between groups, multi-step logic)
- metric: journeys | searches (number of search events) | users | apps | avg_searches
  | avg_duration | total_duration | purchase_rate (share of an app's OWN journeys that ended in a purchase)
- share: true when the question asks for a share OF ALL journeys/purchases ("purchase
  share", "share of purchases", "market share"); focus_apps = the app(s) being compared
- start_apps: journeys whose first search was on these apps ("start on Amazon")
- group_by: app | user | outcome | status | date | hour | query | apps_per_journey
  | start_app | none
- filters: user_ids, journey_ids, apps (exact names above), status, outcome,
  multi_app (yes = used more than one app), date_from/date_to (YYYY-MM-DD),
  query_text (words to match in the search phrase)
- order desc for most/top, asc for least; limit only when a number is given.

Use the resolved entities given with the question; they come from earlier turns.

Examples:
Q: which app has the most search events -> {{"intent":"aggregate","metric":"searches","group_by":"app","order":"desc"}}
Q: apps used by user 5330 -> {{"intent":"aggregate","metric":"searches","group_by":"app","user_ids":[5330]}}
Q: how many journeys ended in purchase on Meesho -> {{"intent":"aggregate","metric":"journeys","group_by":"none","apps":["Meesho"],"outcome":"purchased"}}
Q: which users searched for sneakers the most -> {{"intent":"aggregate","metric":"searches","group_by":"user","query_text":"sneakers"}}
Q: purchase rate per app -> {{"intent":"aggregate","metric":"purchase_rate","group_by":"app"}}
Q: Amazon's purchase share and apps ahead of it -> {{"intent":"aggregate","metric":"journeys","group_by":"app","outcome":"purchased","share":true,"focus_apps":["Amazon"]}}
Q: of journeys starting on Amazon how many buy elsewhere -> {{"intent":"start_funnel","start_apps":["Amazon"]}}
Q: show journeys of user 63 that timed out -> {{"intent":"list_journeys","user_ids":[63],"outcome":"timeout"}}
Q: what did people pay for headphones -> {{"intent":"unsupported"}}
Q: write Python code for factorial -> {{"intent":"out_of_scope"}}
Q: tell me a joke -> {{"intent":"out_of_scope"}}
"""


class LLMPlanner:
    def __init__(self, llm_complete, model: str, summary: dict, apps: AppMatcher):
        self._complete = llm_complete
        self.model = model
        self.summary = summary
        self.apps = apps

    def plan(self, question: str, res: Resolution, hint: QueryPlan | None,
             history_text: str = "") -> QueryPlan | None:
        system = PLANNER_SYSTEM.format(apps=", ".join(self.summary["apps"]),
                                       first_date=self.summary.get("first_date"),
                                       last_date=self.summary.get("last_date"))
        lines = []
        if history_text:
            lines.append("Recent conversation:\n" + history_text)
        lines.append(f"Question: {question}")
        ents = []
        if res.user_ids:
            ents.append(f"user_ids={res.user_ids}")
        if res.journey_ids:
            ents.append(f"journey_ids={res.journey_ids}")
        if res.apps:
            ents.append(f"apps={res.apps}")
        if ents:
            lines.append("Resolved entities: " + ", ".join(ents))
        if hint and hint.intent:
            lines.append("Keyword parser guess (may be incomplete): " +
                         json.dumps(hint.to_dict(), default=str))
        raw = self._complete(system=system, user="\n".join(lines), model=self.model,
                             max_tokens=300, schema=PLAN_SCHEMA)
        try:
            data = json.loads(_extract_json(raw))
        except (json.JSONDecodeError, TypeError):
            return None
        return self._validate(data, res)

    def _validate(self, data: dict, res: Resolution) -> QueryPlan | None:
        intent = data.get("intent")
        if intent not in INTENTS:
            return None
        plan = QueryPlan(intent=intent, source="llm", confidence=0.8)
        metric = data.get("metric")
        plan.metric = metric if metric in METRICS else None
        group = data.get("group_by")
        plan.group_by = group if group in GROUPS else None
        # Entities: trust deterministic resolution over the model when both exist.
        plan.user_ids = res.user_ids or [int(u) for u in data.get("user_ids") or []
                                         if str(u).lstrip("-").isdigit()][:20]
        plan.journey_ids = res.journey_ids or [str(j).upper() for j in data.get("journey_ids") or []
                                               if re.fullmatch(r"J\d{3,}", str(j).upper())]
        apps = []
        for name in data.get("apps") or []:
            apps.extend(self.apps.names(str(name)))
        plan.apps = res.apps or list(dict.fromkeys(apps))

        def app_list(key):
            found = []
            for name in data.get(key) or []:
                found.extend(self.apps.names(str(name)))
            return list(dict.fromkeys(found))

        plan.start_apps = app_list("start_apps")
        plan.apps = [a for a in plan.apps if a not in plan.start_apps]
        if data.get("share") is True:
            plan.share = True
            plan.group_by = plan.group_by or "app"
            plan.focus_apps = app_list("focus_apps") or (plan.apps if plan.group_by == "app" else [])
            if plan.group_by == "app":
                plan.apps = []
        if data.get("status") in ("open", "closed"):
            plan.status = data["status"]
        if data.get("outcome") in ("purchased", "timeout"):
            plan.outcome = data["outcome"]
        if data.get("multi_app") == "yes":
            plan.multi_app = True
        elif data.get("multi_app") == "no":
            plan.multi_app = False
        for attr in ("date_from", "date_to"):
            value = str(data.get(attr) or "")
            if re.fullmatch(r"20\d\d-\d\d-\d\d", value):
                setattr(plan, attr, value)
        text = str(data.get("query_text") or "").strip()
        if text and len(text) <= 80:
            plan.query_text = text
        plan.order = "asc" if data.get("order") == "asc" else "desc"
        limit = data.get("limit")
        if isinstance(limit, int) and 0 < limit <= 500:
            plan.limit = limit
        if plan.intent == "aggregate" and not plan.metric:
            plan.metric = "searches" if plan.group_by == "app" else "journeys"
        if plan.intent == "user_detail" and not plan.user_ids:
            return None
        if plan.intent in ("journey_detail", "similar") and not plan.journey_ids:
            return None
        if plan.intent == "start_funnel" and not plan.start_apps:
            plan.start_apps = plan.apps[:1]
            plan.apps = plan.apps[1:]
            if not plan.start_apps:
                return None
        if plan.intent == "unsupported":
            plan.reason = "fields that are not in this file"
        return plan


def _extract_json(raw: str) -> str:
    raw = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start:end + 1] if start >= 0 and end > start else raw


# --------------------------------------------------------------------------- #
# Compiler: QueryPlan -> parameterised SQL
# --------------------------------------------------------------------------- #

_GROUP_SQL = {
    "app": ("ja.app_name", "app"),
    "user": ("j.user_assoc_id", "user"),
    "outcome": ("COALESCE(j.close_reason, j.status)", "outcome"),
    "status": ("j.status", "status"),
    "date": ("j.search_date", "date"),
    "hour": ("j.search_hour", "hour"),
    "query": ("j.query_cluster_rep", "query"),
    "apps_per_journey": ("j.total_apps", "apps_per_journey"),
    "start_app": ("j.start_app", "start_app"),
}

_METRIC_COLUMN = {
    "journeys": "journeys", "searches": "searches", "users": "users", "apps": "apps",
    "avg_searches": "avg_searches_per_journey", "avg_duration": "avg_duration_minutes",
    "total_duration": "total_duration_minutes",
    "purchase_rate": "purchase_rate_pct",
}


def _where(plan: QueryPlan, pair_mode: bool) -> tuple[list[str], list]:
    clauses, params = [], []
    if plan.user_ids:
        clauses.append(f"j.user_assoc_id IN ({','.join('?' * len(plan.user_ids))})")
        params.extend(plan.user_ids)
    if plan.journey_ids:
        clauses.append(f"j.journey_id IN ({','.join('?' * len(plan.journey_ids))})")
        params.extend(plan.journey_ids)
    if plan.start_apps:
        clauses.append(f"j.start_app IN ({','.join('?' * len(plan.start_apps))})")
        params.extend(plan.start_apps)
    if plan.apps:
        marks = ",".join("?" * len(plan.apps))
        if pair_mode:
            clauses.append(f"ja.app_name IN ({marks})")
        else:
            clauses.append(f"EXISTS (SELECT 1 FROM journey_app f WHERE f.journey_id = j.journey_id "
                           f"AND f.app_name IN ({marks}))")
        params.extend(plan.apps)
    if plan.outcome:
        clauses.append("j.close_reason = ?")
        params.append(plan.outcome)
    elif plan.status:
        clauses.append("j.status = ?")
        params.append(plan.status)
    if plan.multi_app is True:
        clauses.append("j.total_apps > 1")
    elif plan.multi_app is False:
        clauses.append("j.total_apps = 1")
    if plan.date_from:
        clauses.append("j.search_date >= ?")
        params.append(plan.date_from)
    if plan.date_to:
        clauses.append("j.search_date <= ?")
        params.append(plan.date_to)
    if plan.query_text:
        clauses.append("j.query_cluster_rep LIKE ? ESCAPE '\\' COLLATE NOCASE")
        params.append(f"%{escape_like(plan.query_text)}%")
    return clauses, params


def compile_aggregate(plan: QueryPlan, row_cap: int = 500) -> tuple[str, list, str]:
    """Return (sql, params, sort_column)."""
    pair_mode = plan.group_by == "app" or plan.metric == "apps"
    clauses, params = _where(plan, pair_mode)
    if pair_mode:
        source = "journey j JOIN journey_app ja ON ja.journey_id = j.journey_id"
        searches = "ja.event_count"
        app_col = "ja.app_name"
    else:
        source = "journey j"
        if plan.apps:
            marks = ",".join("?" * len(plan.apps))
            searches = (f"(SELECT COALESCE(SUM(s.event_count), 0) FROM journey_app s "
                        f"WHERE s.journey_id = j.journey_id AND s.app_name IN ({marks}))")
            params = list(plan.apps) + params           # searches expr comes first in SQL
        else:
            searches = "j.total_events"
        app_col = "NULL"
    group_expr, group_label = _GROUP_SQL.get(plan.group_by or "", (None, None))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    base = (f"SELECT j.journey_id, j.user_assoc_id, j.close_reason, j.status, j.duration_seconds, "
            f"{app_col} AS app_name, {searches} AS searches"
            + (f", {group_expr} AS grp" if group_expr else "")
            + f" FROM {source} {where}")
    measures = [
        "COUNT(DISTINCT journey_id) AS journeys",
        "COALESCE(SUM(searches), 0) AS searches",
        "COUNT(DISTINCT user_assoc_id) AS users",
        "COUNT(DISTINCT CASE WHEN close_reason = 'purchased' THEN journey_id END) AS purchased",
        "COUNT(DISTINCT CASE WHEN close_reason = 'timeout' THEN journey_id END) AS timed_out",
        "COUNT(DISTINCT CASE WHEN status = 'open' THEN journey_id END) AS open_journeys",
    ]
    if pair_mode:
        measures.append("COUNT(DISTINCT app_name) AS apps")
    if plan.metric == "avg_searches":
        measures.append("ROUND(1.0 * COALESCE(SUM(searches), 0) / COUNT(DISTINCT journey_id), 2) "
                        "AS avg_searches_per_journey")
    if plan.metric == "avg_duration":
        measures.append("ROUND(AVG(duration_seconds) / 60.0, 1) AS avg_duration_minutes")
        measures.append("ROUND(MAX(duration_seconds) / 60.0, 1) AS max_duration_minutes")
    if plan.metric == "total_duration":
        measures.append("ROUND(SUM(duration_seconds) / 60.0, 1) AS total_duration_minutes")
    if plan.metric == "purchase_rate":
        measures.append("ROUND(100.0 * COUNT(DISTINCT CASE WHEN close_reason = 'purchased' "
                        "THEN journey_id END) / COUNT(DISTINCT journey_id), 1) AS purchase_rate_pct")
    sort_col = _METRIC_COLUMN.get(plan.metric or "journeys", "journeys")
    if sort_col == "apps" and not pair_mode:
        sort_col = "journeys"

    if group_expr:
        order = "ASC" if plan.order == "asc" else "DESC"
        tie = "journeys DESC" if sort_col != "journeys" else "searches DESC"
        natural = plan.group_by in ("date", "hour") and plan.limit is None and not (
            plan.singular)
        order_by = f"{group_label} ASC" if natural else f"{sort_col} {order}, {tie}, {group_label}"
        default = {"user": 10, "query": 20}.get(plan.group_by, row_cap)
        limit = min(plan.limit or default, row_cap)
        sql = (f"WITH base AS ({base}) SELECT grp AS {group_label}, {', '.join(measures)} "
               f"FROM base GROUP BY grp ORDER BY {order_by} LIMIT {limit}")
    else:
        sql = f"WITH base AS ({base}) SELECT {', '.join(measures)} FROM base"
    return sql, params, sort_col


def compile_journey_list(plan: QueryPlan, limit: int = 200) -> tuple[str, list, str, list]:
    """Rows of journeys matching the filters, plus a matching COUNT query."""
    clauses, params = _where(plan, pair_mode=False)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "j.first_search_utc, j.journey_id"
    if plan.order == "desc" and (plan.limit or plan.query_text):
        order = "j.total_events DESC, j.first_search_utc"
    sql = (f"SELECT j.journey_id, j.user_assoc_id AS user, j.query_cluster_rep AS query, "
           f"j.apps_summary AS apps, j.total_events AS searches, j.first_search_time AS first_search, "
           f"j.status, COALESCE(j.close_reason, '') AS outcome FROM journey j {where} "
           f"ORDER BY {order} LIMIT {min(plan.limit or limit, limit)}")
    count_sql = f"SELECT COUNT(*) FROM journey j {where}"
    return sql, params, count_sql, list(params)


def compile_start_funnel(plan: QueryPlan) -> dict[str, tuple[str, list]]:
    """Queries for 'what happened to journeys that started on X'."""
    clauses, params = _where(plan, pair_mode=False)
    where = " AND ".join(clauses) or "1=1"
    totals = (f"""SELECT COUNT(*) AS journeys,
                  SUM(j.total_apps = 1) AS stayed,
                  SUM(j.total_apps > 1) AS moved,
                  SUM(j.close_reason = 'purchased') AS purchased,
                  SUM(j.close_reason = 'purchased' AND j.total_apps = 1) AS bought_start_app_only,
                  SUM(j.close_reason = 'purchased' AND j.total_apps > 1) AS bought_after_moving,
                  SUM(j.close_reason = 'timeout') AS timed_out,
                  SUM(j.status = 'open') AS still_open
               FROM journey j WHERE {where}""", list(params))
    marks = ",".join("?" * len(plan.start_apps))
    destinations = (f"""SELECT ja.app_name AS app, COUNT(DISTINCT j.journey_id) AS journeys,
                        SUM(j.close_reason = 'purchased') AS purchased
                        FROM journey j JOIN journey_app ja ON ja.journey_id = j.journey_id
                        WHERE {where} AND j.total_apps > 1 AND ja.app_name NOT IN ({marks})
                        GROUP BY ja.app_name ORDER BY journeys DESC, ja.app_name""",
                    list(params) + list(plan.start_apps))
    return {"totals": totals, "destinations": destinations}
