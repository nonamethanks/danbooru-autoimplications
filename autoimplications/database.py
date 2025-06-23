import re
from collections import defaultdict
from functools import cache
from itertools import batched

from danbooru.models import DanbooruBulkUpdateRequest, DanbooruTag
from peewee import BooleanField, CharField, DateTimeField, DoesNotExist, IntegerField, Model, SqliteDatabase

from autoimplications import logger
from autoimplications.bigquery import clone_bigquery_table

tag_database = SqliteDatabase("data/tags.sqlite")


class BaseModel(Model):
    class Meta:
        database = tag_database
        legacy_table_names = False


class DatabaseRelatedTags(BaseModel):
    name = CharField(index=True, unique=True)
    related_copyrights = CharField()


class DatabaseBurs(BaseModel):
    id = IntegerField(primary_key=True)
    script = CharField()
    status = CharField()
    updated_at = DateTimeField(index=True)

    @property
    def imply_lines(self) -> list[str]:
        lines = [re.sub(r"\s+", " ", line.strip()).strip().lower() for line in self.script.split("\n")]  # type: ignore[attr-defined]
        return [line for line in lines if line.startswith(("create implication", "imply"))]

    @property
    def implications(self) -> dict[str, list[dict[str, str]]]:
        parsed: dict[str, list[dict[str, str]]] = defaultdict(list)
        for bur_line in self.imply_lines:
            line = bur_line.removeprefix("create implication").removeprefix("imply").strip()

            try:
                antecedent, consequent = line.split(" -> ")
            except ValueError as e:
                e.add_note(f"On '{line}'")
                raise

            if " " in antecedent or " " in consequent:
                raise NotImplementedError(antecedent, consequent, bur_line)

            parsed[antecedent] += [{consequent: self.status}]  # type: ignore[dict-item]

        return parsed

    @cache
    @staticmethod
    def implication_map() -> dict[str, list[dict[str, str]]]:
        implication_map: dict[str, list[dict[str, str]]] = defaultdict(list)
        for bur in DatabaseBurs.select():
            for tag_name, implications in bur.implications.items():
                implication_map[tag_name] += implications
        return implication_map

    @classmethod
    def implication_was_already_requested(cls, from_: str, to: str) -> bool:
        implication_map = cls.implication_map()

        if not (implications := implication_map.get(from_)):
            return False

        return any(next(iter(implication.keys())) == to for implication in implications)

    @classmethod
    def tag_has_pending_implication(cls, tag_name: str) -> bool:
        implication_map = cls.implication_map()

        if not (implications := implication_map.get(tag_name)):
            return False

        return any(next(iter(implication.values())) == "pending" for implication in implications)


class DatabaseTags(BaseModel):
    id = IntegerField(primary_key=True)
    name = CharField(index=True)
    post_count = IntegerField(index=True)
    created_at = DateTimeField(index=True)
    updated_at = DateTimeField(index=True)
    is_deprecated = BooleanField(index=True)

    @staticmethod
    def get_tags_from_names(tag_names: list[str] | None = None, tag_ids: list[int] | None = None, **kwargs) -> list[DanbooruTag]:
        if not tag_ids:
            if not tag_names:
                raise ValueError("Tag names are required.")

            chartags = DatabaseTags.select(DatabaseTags.id)\
                .where(DatabaseTags.name << tag_names)\
                .dicts()

            tag_ids = [chartag["id"] for chartag in chartags]
            if not tag_ids:
                return []

        tags: list[DanbooruTag] = []
        for tag_id_group in batched(tag_ids, 100):
            tags += DanbooruTag.get_all(
                id=",".join(map(str, tag_id_group)),
                category=4,
                order="id",
                hide_empty=True,
                is_deprecated=False,
                include="antecedent_implications,wiki_page",
                **kwargs,
            )
        return tags


COPYRIGHT_MAP = {tag["name"]: tag["related_copyrights"].split(",") for tag in DatabaseRelatedTags.select().dicts()}


def update_database() -> None:
    with tag_database:
        tag_database.create_tables([DatabaseRelatedTags, DatabaseBurs, DatabaseTags])

    clone_bigquery_table("tags",
                         model=DatabaseTags,
                         database=tag_database,
                         where="post_count > 0 AND category = 4")

    logger.info("Updating BUR DB.")
    try:
        last_checked_bur = DatabaseBurs.select().order_by(DatabaseBurs.updated_at.desc()).get()
    except DoesNotExist:
        bur_pages = DanbooruBulkUpdateRequest.all_pages(order="updated_at_asc")
    else:
        dt_str = last_checked_bur.updated_at.replace(" ", "T")
        bur_pages = DanbooruBulkUpdateRequest.all_pages(updated_at=f">{dt_str}", order="updated_at_asc")

    for page in bur_pages:
        rows = [
            {
                "id": bur.id,
                "script": bur.script,
                "updated_at": bur.updated_at,
                "status": bur.status,
            }
            for bur in page
        ]
        logger.info(f"Inserting or updating {len(rows)} BURs.")
        with tag_database.atomic():
            DatabaseBurs.replace_many(rows).execute()
