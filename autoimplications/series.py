
from __future__ import annotations

import ast
import operator
import re
from collections import defaultdict
from functools import cached_property, reduce
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from danbooru.models import DanbooruBulkUpdateRequest, DanbooruTag, DanbooruWikiPage
from peewee import DoesNotExist
from pydantic import BaseModel, Field

from autoimplications import logger
from autoimplications.database import COPYRIGHT_MAP, DatabaseBurs, DatabaseRelatedTags, DatabaseTags
from autoimplications.exceptions import TooManyBursError

if TYPE_CHECKING:
    from autoimplications.implication_group import ImplicationGroup


DEFAULT_COSTUME_PATTERN = re.compile(r"(?P<base_name>[^(]+)(?P<qualifiers>(?:_\(.*\)))")

BOT_DISCLAIMER = "[tn]This is an automatic post. Use topic #31779 to report errors/false positives or general feedback.[/tn]"

BOT_IMPLICATION_REASON = """
 [code]beep boop[/code]
"""


class Series(BaseModel):
    name: str
    topic_id: int

    wiki_ids: list[int] = Field(default_factory=list)

    extra_costume_patterns: list[re.Pattern]
    extra_qualifiers: list[str] = Field(default_factory=list)

    line_blacklist: list[str] = Field(default_factory=list)
    qualifier_blacklist: list[str] = Field(default_factory=list)

    grep: str | None = None

    autopost: bool = False

    MAX_BURS_PER_TOPIC: int = 10
    POSTED_BURS: int = 0

    group_by_qualifier: bool = True
    allow_sub_implications: bool = True

    def __hash__(self) -> int:
        return hash(f"{self.topic_id}-{self.name}")

    @cached_property
    def costume_patterns(self) -> list[re.Pattern[str]]:
        return [*self.extra_costume_patterns, DEFAULT_COSTUME_PATTERN]

    @property
    def series_qualifiers(self) -> list[str]:
        return [self.name, *self.extra_qualifiers]

    @cached_property
    def qualifiers_pattern(self) -> str:
        all_qualifiers = "|".join([re.escape(n) for n in self.series_qualifiers])

        qualifier_group = rf"(?:{all_qualifiers})"
        return qualifier_group

    @property
    def topic_url(self) -> str:
        return f"https://danbooru.donmai.us/forum_topics/{self.topic_id}"

    @cached_property
    def topic_bur_count(self) -> int:
        return len(DanbooruBulkUpdateRequest.get(
            limit=self.MAX_BURS_PER_TOPIC,
            forum_topic_id=self.topic_id,
            status="pending",
        ))

    @property
    def remaining_bur_slots(self) -> int:
        return self.MAX_BURS_PER_TOPIC - self.topic_bur_count - self.POSTED_BURS

    @cached_property
    def all_tags_from_search(self) -> list[DanbooruTag]:
        tags: list[DanbooruTag] = []
        for qualifier in self.series_qualifiers:
            tags += DanbooruTag.get_all(
                name_matches=f"*_({qualifier})",
                category=4,
                order="id",
                hide_empty=True,
                is_deprecated=False,
                include="antecedent_implications,wiki_page",
            )

        return tags

    @cached_property
    def all_tags_from_wiki(self) -> list[DanbooruTag]:
        if not self.wiki_ids:
            return []

        all_tags_from_search = self.all_tags_from_search

        wiki_pages = DanbooruWikiPage.get_all(id=",".join(map(str, self.wiki_ids)))
        logger.info(f"Fetching tags for {self.name} from wiki pages {[wiki.url for wiki in wiki_pages]}...")

        tag_names = []
        for wiki in wiki_pages:
            logger.info(f"Processing wiki page '{wiki.title}'")
            tag_names += wiki.linked_tags
            assert tag_names
            tag_names = [t for t in tag_names if t not in [t.name for t in all_tags_from_search]]
            if not tag_names:
                continue
            logger.info(f"Found {len(tag_names)} in wikis so far.")

        if not tag_names:
            return []

        tags = DatabaseTags.get_tags_from_names(tag_names)

        processed_tags = []
        logger.info("Filtering out extraneous tags...")

        for tag in tags:
            if tag.post_count < 5:  # skip small tags
                continue
            if not self.belongs_to_series(tag):
                logger.debug(f"Tag {tag.name} does not belong to {self.name}. Skipping...")
                continue
            processed_tags += [tag]
        logger.info(f"Remaining: {len(processed_tags)}.")

        processed_tags += self.get_child_tags_from_db(processed_tags)

        logger.info(f"Done processing tags from wikis, {len(processed_tags)} found.")
        return processed_tags

    def get_child_tags_from_db(self, parent_tags: list[DanbooruTag]) -> list[DanbooruTag]:
        child_ids: list[int] = []
        clauses = [DatabaseTags.name.startswith(tag.name) for tag in parent_tags]
        for batch in batched(clauses, 50):
            children = DatabaseTags.select() \
                .where(reduce(operator.or_, batch)) \
                .where(~(DatabaseTags.name << [t.name for t in parent_tags]))
            child_ids += [child["id"] for child in children.dicts()]

        if not child_ids:
            return []

        child_tags = DatabaseTags.get_tags_from_names(
            tag_ids=child_ids,
            has_antecedent_implications=False,
        )

        vetted_child_tags = []

        for child_tag in child_tags:
            if not self.belongs_to_series(child_tag):
                logger.debug(f"Potential child tag {child_tag.name} does not belong to {self.name}. Skipping...")
                continue
            vetted_child_tags += [child_tag]

        return vetted_child_tags

    @cached_property
    def all_tag_map(self) -> dict[str, DanbooruTag]:
        return {t.name: t for t in self.all_tags_from_wiki + self.all_tags_from_search}

    def get_parent_for_tag(self, tag: DanbooruTag) -> DanbooruTag | None:
        if tag.antecedent_implications:
            return None

        if set(tag.qualifiers) & set(self.qualifier_blacklist):
            return None

        possible_parents = self.get_possible_parents(tag)

        if possible_parents is False:
            return None

        if not possible_parents:
            logger.trace(f"Could not determine a parent for {tag.name}")
            return None

        possible_parents.sort(key=lambda t: len(t), reverse=self.allow_sub_implications)
        for parent_name in possible_parents:
            if f"{tag.name} -> {parent_name}" in self.line_blacklist:
                logger.trace(f"Skipping {tag.name} -> {parent_name} because this implication was blacklisted.")
                continue

            if not (parent := self.all_tag_map.get(parent_name)):
                continue

            if parent.is_deprecated:
                continue

            return parent

        logger.trace(f"Could not find an existing parent for {tag.name}")
        return None

    def should_skip_implication(self, _from: DanbooruTag, to: DanbooruTag) -> bool:
        if to.name in DatabaseBurs.implication_map().get(_from.name, []):
            logger.trace(f"Skipping {_from.name} -> {to.name} because this implication was already requested.")
            return True

        return False

    @cached_property
    def implication_groups(self) -> list[ImplicationGroup]:
        logger.debug(
            f"{len(self.all_tags_from_wiki)} (from wiki) + {len(self.all_tags_from_search)} (from search) = "
            f"{len(self.all_tags_from_wiki) + len(self.all_tags_from_search)} tags to process.",
        )

        parent_children_map: defaultdict[DanbooruTag, list[DanbooruTag]] = defaultdict(list)
        for tag in self.all_tag_map.values():
            if not (parent := self.get_parent_for_tag(tag)):
                continue

            if self.should_skip_implication(_from=tag, to=parent):
                continue

            parent_children_map[parent] += [tag]

        from autoimplications.implication_group import ImplicationGroup  # noqa: PLC0415

        implication_groups = [ImplicationGroup(main_tag=main_tag, subtags=subtags, series=self)
                              for main_tag, subtags in parent_children_map.items()]
        implication_groups.sort(key=lambda x: x.main_tag.name)

        return implication_groups

    @cached_property
    def implication_groups_by_qualifier(self) -> list[list[ImplicationGroup]]:
        # attempt to group implication groups by qualifier if they're single-tag groups
        grouped_by_qualified: list[list[ImplicationGroup]] = []
        grouped_by_character: list[list[ImplicationGroup]] = []

        qualifier_map: defaultdict[ImplicationGroup, list[str]] = defaultdict(list)
        qualifier_count: defaultdict[str, int] = defaultdict(int)

        for group in self.implication_groups:
            if len(group.subtags) > 1:
                grouped_by_character += [[group]]
                continue

            inserted = False
            for qualifier in group.subtags[0].qualifiers:
                if qualifier in self.series_qualifiers:
                    continue
                qualifier_map[group].append(qualifier)
                qualifier_count[qualifier] += 1
                inserted = True

            if not inserted:
                grouped_by_character += [[group]]

        for qualifier, _ in sorted(qualifier_count.items(), key=lambda item: item[1], reverse=True):
            by_qualifier = [group for group in qualifier_map if qualifier in qualifier_map[group]]
            if len(by_qualifier) > 1:
                grouped_by_qualified += [by_qualifier]
            elif by_qualifier:
                grouped_by_character += [by_qualifier]

            for group in by_qualifier:
                del qualifier_map[group]

        if len(qualifier_map) > 0:
            for ig, leftover_qualifiers in qualifier_map.items():
                subtags = [subtag.name for subtag in ig.subtags]
                logger.warning(f"Leftover qualifier {leftover_qualifiers} -> {subtags}. This should not happen.")
            raise NotImplementedError("Shouldn't be anything else left.")

        total = grouped_by_qualified + sorted(grouped_by_character, key=lambda g: g[0].main_tag.name)
        return total

    @cached_property
    def implicable_tags_without_wiki(self) -> list[DanbooruTag]:
        tags = [t for ig in self.implication_groups for t in ig.tags_without_wiki]
        tags.sort(key=lambda tag: tag.name)
        return tags

    def scan_and_post(self, max_lines_per_bur: int = 1) -> None:
        logger.info(f"Processing series: {self.name}. Topic: {self.topic_url}.")
        logger.info(f"There are {len(self.implication_groups)} implication groups. "
                    f"Max lines per BUR: {max_lines_per_bur}.")
        logger.info(f"Remaining amount of BURs that can be posted {self.topic_url}: {self.remaining_bur_slots}.")

        counter = max_lines_per_bur
        bur_script = ""
        tags_with_no_wikis = []

        posted = []

        groups_by_qualifier = self.implication_groups_by_qualifier if self.group_by_qualifier \
            else [[group] for group in self.implication_groups]

        for qualifier_groups in groups_by_qualifier:
            for group in qualifier_groups:
                logger.debug(f"Found implication group: {", ".join(tag.name for tag in group.subtags)} -> {group.main_tag.name} ")
                if group.tags_without_wiki:
                    logger.debug(f"There are {len(group.tags_without_wiki)} tags without a wiki here: {group.tags_without_wiki}.")
                    tags_with_no_wikis += group.tags_without_wiki

                if not group.tags_with_wiki:
                    logger.debug("<r>This group has no tags with wiki. Moving on...</r>")
                    continue

                counter -= len(group.tags_with_wiki)
                bur_script += group.script + "\n"

            if counter <= 0:
                self.send_bur(bur_script)
                posted += [bur_script]
                bur_script = ""
                counter = max_lines_per_bur

        if bur_script:
            self.send_bur(bur_script)
            posted += [bur_script]

        logger.info(f"In total, {len(posted)} BURs {"would " if not self.autopost else ""}have been submitted.")
        if len(posted):
            burs = [f"[expand BUR #{index+1}]\n{"\n".join(sorted(bur.splitlines()))}\n[/expand]"
                    for index, bur in enumerate(posted)]
            logger.info("\n\n" + "\n\n".join(burs))

        logger.info(f"Topic of submission: {self.topic_url}")
        logger.info(f"Reason for BURs: {self.bur_reason}")

    @cached_property
    def bur_reason(self) -> str:
        return BOT_IMPLICATION_REASON + "\n" + wikiless_tags_to_dtext(self.implicable_tags_without_wiki) + "\n" + BOT_DISCLAIMER

    def matches(self, name: str) -> bool:
        bad_chars = "!?."
        matches = [
            name.strip(bad_chars).replace("_", " ")
            for name in [self.name, *self.extra_qualifiers]
        ]
        return name.strip(bad_chars).replace("_", " ") in matches

    def send_bur(self, script: str) -> None:

        logger.info("Submitting implications:")

        script = "\n".join(sorted(script.splitlines()))

        logger.info(f"\n<c>{script}</c>")

        if self.autopost:
            if self.remaining_bur_slots <= 0:
                raise TooManyBursError
            DanbooruBulkUpdateRequest.create(
                forum_topic_id=self.topic_id,
                script=script,
                reason=self.bur_reason,
            )
            self.POSTED_BURS += 1

    def belongs_to_series(self, tag: DanbooruTag) -> bool:
        if tag.has_series_qualifier(self.series_qualifiers):
            return True

        if not (known_copyrights := COPYRIGHT_MAP.get(tag.name)):
            logger.debug(f"Searching for copyright for tag {tag.name}...")
            related_copyrights = tag.related_copyrights
            if not related_copyrights:
                logger.error(f"Copyrights for tag {tag.name} could not be determined.")
                return False
            try:
                saved = DatabaseRelatedTags.get(DatabaseRelatedTags.name == tag.name)
            except DoesNotExist:
                saved = DatabaseRelatedTags(name=tag.name)
            saved.related_copyrights = ",".join(t.name for t in related_copyrights)
            logger.debug(f"Saving copyright for {tag.name} to database...")
            saved.save()
            known_copyrights = saved.related_copyrights.split(",")  # type: ignore[attr-defined]
            COPYRIGHT_MAP[tag.name] = known_copyrights
            assert known_copyrights

        return any(qualifier in known_copyrights for qualifier in self.series_qualifiers)

    def get_possible_parents(self, tag: DanbooruTag) -> list[str] | Literal[False]:
        all_possible_parents: list[str] = []

        matched_count = 0
        for pattern in self.costume_patterns:
            if not (match := pattern.match(tag.name)):
                continue

            matched_count += 1
            possible_parents = []

            base_name = match.groupdict()["base_name"]
            extra_qualifier = match.groupdict().get("extra_qualifier")
            try:
                qualifiers = re.findall(r"(\(.*?\))", match.groupdict()["qualifiers"])
            except Exception as e:
                raise NotImplementedError(tag, pattern, match.groupdict()) from e

            qualifiers = [q.strip("_") for q in [extra_qualifier, *qualifiers] if q]

            series_qualifier_pattern = rf"_\({self.qualifiers_pattern}\)$"
            if re.search(series_qualifier_pattern, tag.name):
                [*qualifiers, series_qualifier] = qualifiers
            else:
                series_qualifier = None

            for index in range(len(qualifiers)):
                partial_qualifier = "_".join(qualifiers[:index])

                possible_parent = f"{base_name}_{partial_qualifier}"
                if series_qualifier:
                    possible_parent = f"{possible_parent}_{series_qualifier}"

                possible_parent = re.sub(r"_+", "_", possible_parent).strip("_")
                if possible_parent == tag.name:
                    continue

                possible_parents.append(possible_parent)

            if len(possible_parents) == 0:
                matched_count -= 1
            all_possible_parents += possible_parents

        if not matched_count:
            return False

        return list(dict.fromkeys(all_possible_parents))

    @staticmethod
    def from_config(grep: str | None = None) -> list[Series]:
        config_path = Path("config.yaml")
        autoimplication_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        series_list = [
            Series(grep=grep, **series | {
                "extra_costume_patterns": [ast.literal_eval(p) for p in series.get("extra_costume_patterns", [])],
            })
            for series in autoimplication_config["series"]
        ]

        return series_list


def wikiless_tags_to_dtext(tags: list[DanbooruTag]) -> str:
    if not tags:
        return ""

    body = "\n[expand Tags without a wiki that couldn't be submitted]"
    for tag in tags:
        body += f"\n* [[{tag.name}]]"
    body += "\n[/expand]\n"

    will_be_batched = len(tags) > 100

    for index, tag_batch in enumerate(batched(tags, 100)):
        link = f"/tags?search[has_wiki_page]=no&limit=100&search[id]={",".join(map(str, (t.id for t in tag_batch)))}"
        link_number = f" #{index+1}" if will_be_batched else ""
        link_description = f"Link{link_number} to tags that couldn't be submitted"
        body += f'\n* "{link_description}":{link}'

    return body
