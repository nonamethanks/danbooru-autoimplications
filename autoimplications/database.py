import re
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

    @staticmethod
    def implications_for(tag_name: str, status: str | None = None) -> list[str]:
        burs = DatabaseBurs.select()
        burs = burs.where(DatabaseBurs.script.contains(tag_name))
        if status:
            burs = burs.where(DatabaseBurs.status == status)

        consequents = []

        for bur_line in [line for bur in burs for line in bur.imply_lines]:
            line = bur_line.removeprefix("create implication").removeprefix("imply").strip()

            try:
                antecedent, consequent = line.split(" -> ")
            except ValueError as e:
                e.add_note(f"On '{line}'")
                raise

            if " " in antecedent or " " in consequent:
                raise NotImplementedError(antecedent, consequent, bur_line)

            if antecedent != tag_name:
                continue

            consequents.append(consequent)

        return consequents

    @classmethod
    def implication_was_already_requested(cls, from_: str, to: str) -> bool:
        if not (consequents := cls.implications_for(from_)):
            return False

        return any(implication == to for implication in consequents)

    @classmethod
    def tag_has_pending_implication(cls, tag_name: str) -> bool:
        return bool(cls.implications_for(tag_name, status="pending"))


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

    update_bur_db()


def update_bur_db() -> None:
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
