package den.android

import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.isImeVisible
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext

class MainActivity : ComponentActivity() {
    private val model: AppModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        // Draw behind the system bars (this also sets decorFitsSystemWindows to false), so the
        // keyboard arrives as an inset the layout handles itself; the manifest asks for
        // adjustResize so it is reported at all.
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContent { DenTheme { DenApp(model) } }
    }

    override fun onStart() {
        super.onStart()
        // Coming back from the background: the SSH session may have died while we were away.
        model.onForeground()
    }
}

private val TABS = listOf("Connect", "Status", "Chat", "Ask", "Image", "Poses", "Voices")
private val GLYPHS = listOf("⚿", "◉", "✎", "?", "▣", "♙", "♪")

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun DenApp(model: AppModel) {
    var tab by rememberSaveable { mutableIntStateOf(0) }
    // While the keyboard is up there is little room, in landscape almost none: the tabs step
    // aside so the field being typed into stays on screen.
    val imeVisible = WindowInsets.isImeVisible
    Scaffold(
        // The one place the window's insets are taken: the status bar, the gesture bar, a
        // display cutout and the keyboard, whichever is largest at the bottom. Everything
        // inside then lays out in what is left, so nothing hides behind the keyboard and
        // nothing is spaced for it twice.
        modifier = Modifier.safeDrawingPadding(),
        contentWindowInsets = WindowInsets(0),
        bottomBar = {
            if (!imeVisible) {
                NavigationBar(windowInsets = WindowInsets(0)) {
                    TABS.forEachIndexed { i, name ->
                        NavigationBarItem(
                            selected = tab == i,
                            onClick = { tab = i },
                            icon = { Text(GLYPHS[i]) },
                            label = { Text(name) },
                        )
                    }
                }
            }
        }
    ) { padding ->
        val modifier = Modifier.padding(padding)
        when (tab) {
            0 -> ConnectionScreen(model, modifier)
            1 -> StatusScreen(model, modifier)
            2 -> ChatScreen(model, modifier)
            3 -> AskScreen(model, modifier)
            4 -> ImageScreen(model, modifier)
            5 -> PosesScreen(model, modifier)
            else -> VoicesScreen(model, modifier)
        }
    }
}

@Composable
fun DenTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = colors, content = content)
}
