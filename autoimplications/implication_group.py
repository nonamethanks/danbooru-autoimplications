from danbooru.models import DanbooruTag
from pydantic import BaseModel

from autoimplications.series import Series


class ImplicationGroup(BaseModel):
    main_tag: DanbooruTag
    subtags: list[DanbooruTag]

    series: Series

    def __hash__(self) -> int:
        return hash(f"{self.main_tag}-{self.subtags}")

    @property
    def script(self) -> str:
        return "\n".join(f"imply {subtag.name} -> {self.main_tag.name}" for subtag in self.tags_with_wiki)

    @property
    def tags_with_wiki(self) -> list[DanbooruTag]:
        return [tag for tag in self.subtags if tag.wiki_page]

    @property
    def tags_without_wiki(self) -> list[DanbooruTag]:
        return [tag for tag in self.subtags if not tag.wiki_page]

    # def create_missing_wikis(self) -> None:
    #     for tag in self.tags_without_wiki:
    #         logger.info(f"Creating wiki page for {tag.name} {tag.url}")
    #         db_api.create_wiki_page(title=tag.name, body=self.wiki_template)

    # @property
    # def wiki_template(self) -> str:
    #     body = f"""
    #     Alternate costume for [[{self.main_tag.name}]].
    #
    #     h4. Appearance
    #     * !post #REPLACEME
    #     """
    #
    #     return remove_indent(body)
