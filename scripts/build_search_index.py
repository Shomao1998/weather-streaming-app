"""Create or update the Azure AI Search index and upload the built chunks.

    python scripts/build_search_index.py --print-schema    # no Azure needed
    python scripts/build_search_index.py --create-index
    python scripts/build_search_index.py --upload

Requires an Azure AI Search service and `AZURE_SEARCH_ENDPOINT`. Authentication
is managed identity or `az login` — there is no key in this script and none in
the repository.

`--print-schema` exists so the index definition is reviewable, diffable and
testable without a subscription. That matters here: the subscription this
project runs on has AI Search removed on cost grounds, and the local retriever
is the supported way to run the feature. This script is the production path,
kept honest by having its schema asserted in tests.

Upload is an idempotent merge-or-upload keyed on the content-addressed
`chunk_id`, so re-running never duplicates a passage. Chunks whose source was
disabled are uploaded too, with `enabled: false`, because the retriever filters
on that field and a citation must stay resolvable after its source is retired.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "functions"))

from weather.advice.knowledge import KnowledgeIndex  # noqa: E402

INDEX_PATH = REPO / "knowledge" / "processed" / "index.json"
DEFAULT_INDEX_NAME = "weather-advice-knowledge"

# Must match `AzureSearchRetriever.build_filter` and the vector field name it
# queries; a mismatch here fails at query time, not at build time.
VECTOR_FIELD = "content_vector"
VECTOR_PROFILE = "default-vector-profile"


def index_schema(index_name: str, dimensions: int) -> dict:
    """The index definition, as plain JSON so it can be diffed and asserted."""
    filterable = (
        "severity", "authority", "jurisdiction", "locale", "version", "source_document_id",
    )
    return {
        "name": index_name,
        "fields": [
            {"name": "chunk_id", "type": "Edm.String", "key": True, "filterable": True},
            {
                "name": "content", "type": "Edm.String",
                "searchable": True, "analyzer": "en.microsoft",
            },
            {"name": "heading", "type": "Edm.String", "searchable": True},
            {"name": "title", "type": "Edm.String", "searchable": True},
            {
                "name": "hazard_types",
                "type": "Collection(Edm.String)",
                "filterable": True,
                "facetable": True,
            },
            *(
                {"name": name, "type": "Edm.String", "filterable": True, "facetable": True}
                for name in filterable
            ),
            {"name": "effective_from", "type": "Edm.String", "filterable": True},
            {"name": "expires_at", "type": "Edm.String", "filterable": True},
            {"name": "last_verified_at", "type": "Edm.String", "filterable": True},
            {"name": "source_url", "type": "Edm.String", "retrievable": True},
            # The retriever asserts `enabled eq true` on every query, so this
            # field must be filterable and must never be omitted from a document.
            {"name": "enabled", "type": "Edm.Boolean", "filterable": True},
            {
                "name": VECTOR_FIELD,
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "dimensions": dimensions,
                "vectorSearchProfile": VECTOR_PROFILE,
            },
        ],
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "default-hnsw",
                    "kind": "hnsw",
                    "hnswParameters": {"m": 4, "efConstruction": 400, "metric": "cosine"},
                }
            ],
            "profiles": [{"name": VECTOR_PROFILE, "algorithm": "default-hnsw"}],
        },
        "semantic": {
            "configurations": [
                {
                    "name": "default",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "heading"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                    },
                }
            ]
        },
    }


def to_documents(index: KnowledgeIndex) -> list[dict]:
    documents = []
    for chunk in index.chunks:
        payload = chunk.to_dict(include_vector=True)
        payload.setdefault("expires_at", "")
        documents.append(payload)
    return documents


def _client(index_name: str):
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient

    endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT")
    if not endpoint:
        raise SystemExit("AZURE_SEARCH_ENDPOINT is not set.")
    return SearchClient(endpoint, index_name, DefaultAzureCredential())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index-name", default=os.environ.get("AZURE_SEARCH_INDEX", DEFAULT_INDEX_NAME)
    )
    parser.add_argument("--source", default=str(INDEX_PATH))
    parser.add_argument("--print-schema", action="store_true")
    parser.add_argument("--create-index", action="store_true")
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args()

    index = KnowledgeIndex.load(args.source)
    dimensions = len(index.chunks[0].content_vector) if index.chunks else 1536
    schema = index_schema(args.index_name, dimensions)

    if args.print_schema or not (args.create_index or args.upload):
        print(json.dumps(schema, indent=2))
        print(
            f"\n# {len(index.chunks)} chunks, {dimensions} dimensions, "
            f"embedder {index.embedding_model}, index_version {index.index_version}",
        )
        if index.embedding_model.startswith("hashing"):
            print(
                "# WARNING: built with the offline hashing embedder. Rebuild with "
                "Azure OpenAI embeddings before uploading to a real service.",
            )
        return 0

    if args.create_index:
        from azure.identity import DefaultAzureCredential
        from azure.search.documents.indexes import SearchIndexClient

        endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT")
        if not endpoint:
            raise SystemExit("AZURE_SEARCH_ENDPOINT is not set.")
        SearchIndexClient(endpoint, DefaultAzureCredential()).create_or_update_index(schema)
        print(f"created or updated index '{args.index_name}'")

    if args.upload:
        if index.embedding_model.startswith("hashing"):
            raise SystemExit(
                "Refusing to upload an index built with the offline hashing embedder. "
                "Set AZURE_OPENAI_ENDPOINT and re-run scripts/ingest_knowledge.py first."
            )
        documents = to_documents(index)
        results = _client(args.index_name).merge_or_upload_documents(documents)
        failed = [r for r in results if not r.succeeded]
        print(f"uploaded {len(documents) - len(failed)}/{len(documents)} chunks")
        if failed:
            for failure in failed[:5]:
                print(f"  failed {failure.key}: {failure.error_message}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
