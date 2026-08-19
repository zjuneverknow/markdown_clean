from markdowncleaner import MarkdownCleaner, CleanerConfig

cleaner = MarkdownCleaner(CleanerConfig(
    window_chars=10000,
    factbase_mode=True,
))

output_path = cleaner.clean(
    r"dataset\马克思主义基本原理（2023版）.md",
    r"clean\马克思主义基本原理（2023版）.md",
)

print(output_path)