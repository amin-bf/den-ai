package den.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LinksTest {
    private fun kinds(text: String) = Links.find(text).map { it.text to it.kind }

    @Test
    fun aUrlAtTheEndOfASentence() {
        assertEquals(
            listOf("https://github.com/amin-bf/den-ai" to LinkKind.URL),
            kinds("The repo is at https://github.com/amin-bf/den-ai."),
        )
    }

    @Test
    fun aGalleryPathIsOnThisPhone() {
        val text = "Done: fake-fast, seed 7. Shown to the user and saved to Pictures/den/chat/a-chat-1/den-20260922-193123-522.png."
        assertEquals(
            listOf("Pictures/den/chat/a-chat-1/den-20260922-193123-522.png" to LinkKind.PHONE_FILE),
            kinds(text),
        )
    }

    @Test
    fun aPathInAnErrorBelongsToTheOtherMachine() {
        val text = "den: ComfyUI didn't start; its log is at /Users/someone/comfy/user/comfyui.log on that machine"
        assertEquals(
            listOf("/Users/someone/comfy/user/comfyui.log" to LinkKind.OTHER_MACHINE_PATH),
            kinds(text),
        )
    }

    @Test
    fun aHomePathIsFoundToo() {
        assertEquals(listOf("~/.ssh/authorized_keys" to LinkKind.OTHER_MACHINE_PATH), kinds("Add it to ~/.ssh/authorized_keys there."))
    }

    @Test
    fun plainTextHasNoLinks() {
        assertEquals(emptyList<Pair<String, LinkKind>>(), kinds("Just words, a ratio of 3/4, and nothing to open."))
        assertEquals(emptyList<Pair<String, LinkKind>>(), kinds(""))
    }

    @Test
    fun aPathInsideAUrlIsNotASecondLink() {
        val found = kinds("See https://example.com/some/deep/path.png for the picture")
        assertEquals(listOf("https://example.com/some/deep/path.png" to LinkKind.URL), found)
    }

    @Test
    fun aMimeTypeOrARatioIsNotAPath() {
        assertEquals(emptyList<Pair<String, LinkKind>>(), kinds("I received 1 image: image/jpeg 1024x682 19 KB."))
        assertEquals(emptyList<Pair<String, LinkKind>>(), kinds("about 3/4 of the way, and/or later"))
        assertEquals(emptyList<Pair<String, LinkKind>>(), kinds("a lone /tmp means little"))
    }

    @Test
    fun splitKeepsTheTextInOrder() {
        val pieces = Links.split("open https://den.example/x now")
        assertEquals(listOf("open ", "https://den.example/x", " now"), pieces.map { it.first })
        assertEquals(listOf(null, LinkKind.URL, null), pieces.map { it.second?.kind })
        assertEquals("open https://den.example/x now", pieces.joinToString("") { it.first })
    }

    @Test
    fun severalLinksInOneMessage() {
        val text = "saved to Pictures/den/chat/c/den-1.png, see https://example.com/help"
        val found = kinds(text)
        assertEquals(2, found.size)
        assertTrue(found[0].second == LinkKind.PHONE_FILE)
        assertTrue(found[1].second == LinkKind.URL)
    }
}
