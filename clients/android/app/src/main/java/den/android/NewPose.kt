package den.android

import android.app.Application
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.util.Base64
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Making a saved pose out of a photo on this phone, in two passes over the den's GPU.
 *
 * The first is `POST /pose` with `"draw_only"`: the broker draws the skeleton, hands it back and
 * keeps nothing, so the skeleton can be looked at beside the photo before it is named — a
 * skeleton with a limb missing, or drawn from a photo with nobody in it, is worth throwing away
 * rather than curating later (ADR image-generation). Saving sends the photo again with a name and a
 * description; that draws a second time on the broker's machine and keeps the pose in its
 * library. No path of either machine crosses: the photo goes as its name and bytes, scaled down
 * here, and the skeleton comes back as bytes (ADR remote-brokers).
 */
class PoseMaker(app: Application) : AndroidViewModel(app) {
    /** The photo, scaled and JPEG-encoded the way an attached picture is. */
    var photo by mutableStateOf<Attached?>(null)
        private set
    var map by mutableStateOf<Bitmap?>(null)
        private set
    /** What the broker said about the map it drew: size, aspect and how long it took. */
    var drawn by mutableStateOf<String?>(null)
        private set
    var drawing by mutableStateOf(false)
        private set
    var saving by mutableStateOf(false)
        private set
    val progress = mutableStateListOf<String>()
    var error by mutableStateOf<String?>(null)
        private set
    /** Set when the name is already a pose there: saving again offers to replace it. */
    var taken by mutableStateOf(false)
        private set
    var saved by mutableStateOf<String?>(null)
        private set
    /** Whether the form is showing; it outlives a tab switch, as a drawing in flight does. */
    var open by mutableStateOf(false)
        private set
    var name by mutableStateOf("")
    var description by mutableStateOf("")

    val busy: Boolean get() = drawing || saving

    /** den's rule for a pose name: lower-case words joined by hyphens (den/poses.py). */
    val nameOk: Boolean get() = name.length <= NAME_MAX && NAME.matches(name)
    val descriptionOk: Boolean get() = description.isNotBlank() && description.length <= DESCRIPTION_MAX

    fun start() {
        open = true
        saved = null
    }

    fun close() {
        clear()
        open = false
    }

    /** Something this phone ran into rather than the broker, e.g. no camera app. */
    fun problem(message: String) {
        error = message
    }

    fun pick(uri: Uri, fallback: String) {
        error = null
        io {
            val attached = Media.attach(getApplication(), uri, fallback)
            if (attached == null) {
                error = "That file isn't an image this phone can read."
            } else {
                photo = attached
                map = null
                drawn = null
                saved = null
                taken = false
                progress.clear()
            }
        }
    }

    /** First pass: draw the skeleton on the den's GPU and keep nothing. */
    fun draw(client: DenClient?) {
        val c = client ?: return
        val source = photo ?: return
        drawing = true
        error = null
        map = null
        drawn = null
        progress.clear()
        io {
            try {
                // No name and no description: nothing is saved, so the broker asks for neither.
                val request = JSONObject().put("image", asBytes(source)).put("draw_only", true)
                val result = c.pose(request) { line ->
                    viewModelScope.launch { progress.add(DenClient.describeProgress(line)) }
                }
                val png = Base64.decode(result.getJSONArray("images").getString(0), Base64.DEFAULT)
                map = BitmapFactory.decodeByteArray(png, 0, png.size)
                drawn = "${result.opt("width")}x${result.opt("height")} · ${result.optString("aspect")} · " +
                    "${result.opt("seconds")} s" + (result.optDouble("waited_s", 0.0).takeIf { it > 0 }?.let { ", waited $it s" } ?: "")
            } catch (e: Exception) {
                error = explain(e, drawOnly = true)
            } finally {
                drawing = false
            }
        }
    }

    /** Second pass: the same photo, now with a name, kept in the library on the broker's machine. */
    fun save(client: DenClient?, replace: Boolean = false, onSaved: () -> Unit) {
        val c = client ?: return
        val source = photo ?: return
        if (!nameOk || !descriptionOk) return
        saving = true
        error = null
        taken = false
        progress.clear()
        io {
            try {
                val request = JSONObject()
                    .put("image", asBytes(source))
                    .put("name", name)
                    .put("description", description.trim())
                    .put("replace", replace)
                val result = c.pose(request) { line ->
                    viewModelScope.launch { progress.add(DenClient.describeProgress(line)) }
                }
                val kept = "Saved as ${result.optString("name", name)} (${result.optString("aspect")}, " +
                    "${result.opt("width")}x${result.opt("height")}), drawn in ${result.opt("seconds")} s."
                close()
                saved = kept
                onSaved()
            } catch (e: Exception) {
                taken = (e.message ?: "").contains("already a saved pose")
                error = explain(e, drawOnly = false)
            } finally {
                saving = false
            }
        }
    }

    /** Throw the drawing away: nothing was saved on the broker's machine, so this is all of it. */
    private fun clear() {
        photo = null
        map = null
        drawn = null
        name = ""
        description = ""
        taken = false
        error = null
        progress.clear()
    }

    private fun asBytes(attached: Attached): JSONObject =
        JSONObject().put("name", attached.name).put("base64", Base64.encodeToString(attached.jpeg, Base64.NO_WRAP))

    /** The broker's message, or a plainer one where a phone can say it better. */
    private fun explain(e: Exception, drawOnly: Boolean): String {
        val message = e.message ?: e.javaClass.simpleName
        return when {
            message.contains("no person found") ->
                "No person found in that photo: the skeleton came out blank, so nothing was kept. " +
                    "Try a photo with one person, whole and clearly visible."
            // A den without draw_only takes the request for a save and stops at the missing name.
            drawOnly && message.contains("a pose name is lower-case") ->
                "This den is too old to draw a pose without saving it. Update den on the broker's " +
                    "machine and restart its broker, then try again."
            message.contains("already a saved pose") ->
                "There is already a pose called $name there. Save again as a replacement, or pick another name."
            else -> message
        }
    }

    private fun io(block: suspend () -> Unit) {
        viewModelScope.launch { withContext(Dispatchers.IO) { block() } }
    }

    private companion object {
        val NAME = Regex("[a-z0-9]+(-[a-z0-9]+)*")
        const val NAME_MAX = 80
        const val DESCRIPTION_MAX = 300
    }
}

/**
 * The Poses tab's "new pose" flow: a photo, the skeleton drawn from it, then the name and
 * description it is kept under. [onSaved] refreshes the list of poses.
 */
@Composable
fun NewPose(model: AppModel, onSaved: () -> Unit, maker: PoseMaker = viewModel()) {
    if (!maker.open) {
        OutlinedButton(onClick = maker::start, Modifier.fillMaxWidth()) { Text("＋ New pose from a photo") }
        maker.saved?.let { Text(it, color = MaterialTheme.colorScheme.primary, style = MaterialTheme.typography.bodySmall) }
        return
    }
    Card {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("New pose", style = MaterialTheme.typography.titleMedium)
            Text(
                "A photo of one person, whole and clearly visible. It is sent at up to ${Media.LONG_SIDE} px; " +
                    "den draws the skeleton on its GPU and keeps nothing until you save.",
                style = MaterialTheme.typography.bodySmall,
            )
            PhotoButtons(model, maker)
            Photos(maker)
            if (maker.photo != null && maker.map == null) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Button(onClick = { maker.draw(model.client) }, enabled = !maker.busy) { Text("Draw the pose") }
                    if (maker.drawing) CircularProgressIndicator()
                }
            }
            maker.progress.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
            if (maker.map != null) NameAndDescription(model, maker, onSaved)
            maker.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            TextButton(onClick = maker::close, enabled = !maker.busy) {
                Text(if (maker.photo == null) "Close" else "Discard")
            }
        }
    }
}

@Composable
private fun PhotoButtons(model: AppModel, maker: PoseMaker) {
    val pick = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) maker.pick(uri, uri.lastPathSegment ?: "photo")
    }
    var target by remember { mutableStateOf<Uri?>(null) }
    val camera = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { taken ->
        if (taken) target?.let { maker.pick(it, "photo.jpg") }
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = { pick.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }, enabled = !maker.busy) {
            Text(if (maker.photo == null) "Pick a photo" else "Another photo")
        }
        OutlinedButton(
            onClick = {
                val uri = model.cameraTarget()
                target = uri
                runCatching { camera.launch(uri) }.onFailure { maker.problem("No camera app on this phone.") }
            },
            enabled = !maker.busy,
        ) { Text("Take one") }
    }
}

/** The photo and, once it has been drawn, the skeleton beside it. */
@Composable
private fun Photos(maker: PoseMaker) {
    val photo = maker.photo ?: return
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
            photo.bitmap?.let {
                Image(it.asImageBitmap(), "the photo", Modifier.fillMaxWidth().heightIn(max = 260.dp), contentScale = ContentScale.Fit)
            }
            Text("photo · ${photo.width}x${photo.height}", style = MaterialTheme.typography.labelSmall)
        }
        maker.map?.let { drawn ->
            Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                Image(drawn.asImageBitmap(), "the skeleton den drew", Modifier.fillMaxWidth().heightIn(max = 260.dp), contentScale = ContentScale.Fit)
                Text("skeleton", style = MaterialTheme.typography.labelSmall)
            }
        }
    }
    maker.drawn?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    if (maker.map != null) {
        Text("Every limb there? A pose with one missing guides an image badly.", style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun NameAndDescription(model: AppModel, maker: PoseMaker, onSaved: () -> Unit) {
    OutlinedTextField(
        maker.name, { maker.name = it }, label = { Text("Name") }, singleLine = true,
        isError = maker.name.isNotEmpty() && !maker.nameOk,
        supportingText = { Text("Lower-case words joined by hyphens, e.g. look-back-hand-on-hip") },
        modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
        maker.description, { maker.description = it }, label = { Text("Description") },
        supportingText = { Text("One line: the pose and the framing, e.g. \"full body, three-quarter from behind, looking back over the shoulder\"") },
        modifier = Modifier.fillMaxWidth(),
    )
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        Button(
            onClick = { maker.save(model.client, replace = false, onSaved = onSaved) },
            enabled = !maker.busy && maker.nameOk && maker.descriptionOk,
        ) { Text("Save") }
        if (maker.taken) {
            Button(
                onClick = { maker.save(model.client, replace = true, onSaved = onSaved) },
                enabled = !maker.busy,
            ) { Text("Replace") }
        }
        if (maker.saving) CircularProgressIndicator()
    }
    Text("Saving draws the pose once more on the den's GPU, then keeps it there.", style = MaterialTheme.typography.bodySmall)
}
