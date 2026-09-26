package den.android

/**
 * What a message's text refers to: a web address, a file on this phone, or a file on the
 * broker's machine. Only the last one can't be opened here, because it is somewhere else
 * (ADR remote-brokers: no path of either machine means anything on the other).
 */
enum class LinkKind { URL, PHONE_FILE, OTHER_MACHINE_PATH }

data class Link(val text: String, val kind: LinkKind, val start: Int, val end: Int)

/**
 * Finds the links in a message or a tool result, with no Android in the way so it can be
 * unit-tested: web addresses, the gallery paths den's own results name
 * (`Pictures/den/chat/…/den-….png`) and absolute paths, which on a remote den belong to that
 * machine.
 */
object Links {
    private val URL = Regex("""https?://[^\s<>"'\\]+""")

    /** What the app itself saved, named the way a tool result names it. */
    private val PHONE_FILE = Regex("""(?<![\w/])(?:Pictures|DCIM|Download|Movies)/[\w.@+-]+(?:/[\w.@+-]+)*\.(?:png|jpe?g|webp|gif)""", RegexOption.IGNORE_CASE)

    /**
     * An absolute path, or one under a home folder: /Users/x/a.png, ~/den/config.toml. Spaces
     * are not taken as part of a name: a path with one is rarer than a path followed by words.
     */
    private val PATH = Regex("""(?<![\w:/])(?:~/|/)(?:[\w.@+-]+/)+[\w.@+-]+""")

    fun find(text: String): List<Link> {
        val found = mutableListOf<Link>()
        fun add(match: MatchResult, kind: LinkKind) {
            val value = match.value.trimEnd('.', ',', ';', ':', '!', '?', ')', ']', '}', '\'', '"')
            if (value.isEmpty()) return
            val range = match.range.first until match.range.first + value.length
            if (found.any { range.first < it.end && it.start < range.last + 1 }) return // inside an earlier link
            found += Link(value, kind, range.first, range.last + 1)
        }
        URL.findAll(text).forEach { add(it, LinkKind.URL) }
        PHONE_FILE.findAll(text).forEach { add(it, LinkKind.PHONE_FILE) }
        PATH.findAll(text).forEach { match ->
            // A bare word or a sentence's slash isn't a path; require a separator and a name.
            if (!match.value.contains('/') || match.value.length < 4) return@forEach
            add(match, LinkKind.OTHER_MACHINE_PATH)
        }
        return found.sortedBy { it.start }
    }

    /** The pieces of a text in order: plain stretches and links. */
    fun split(text: String): List<Pair<String, Link?>> {
        val pieces = mutableListOf<Pair<String, Link?>>()
        var at = 0
        for (link in find(text)) {
            if (link.start > at) pieces += text.substring(at, link.start) to null
            pieces += link.text to link
            at = link.end
        }
        if (at < text.length) pieces += text.substring(at) to null
        return pieces
    }
}
