import pyarrow as pa

VECTOR_DIMENSION = 1024

# The ``position`` field is optional metadata describing where the
# stored text sat in its source file. Tables created before this field
# was introduced simply lack it; writers must tolerate its absence
# rather than rewriting or migrating existing vector data.
KNOWLEDGE_VECTOR_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("text", pa.string()),
    pa.field("source", pa.string()),
    pa.field("position", pa.int64(), nullable=True),
    pa.field("embedding", pa.list_(pa.float32(), VECTOR_DIMENSION)),
])