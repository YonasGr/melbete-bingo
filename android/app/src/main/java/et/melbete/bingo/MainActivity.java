package et.melbete.bingo;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.TextView;

/**
 * Melbete Bingo launcher.
 *
 * The game is a Telegram Mini App: every wallet and game action is verified by Telegram, so it only
 * works inside Telegram. This app gives players a normal Android icon that opens the Melbete bot in
 * Telegram (where the Play button is), and helps them install Telegram if it is missing.
 * It needs no permissions and makes no network requests of its own.
 */
public class MainActivity extends Activity {

    private static final String[] TELEGRAM_PACKAGES = {
            "org.telegram.messenger",       // Telegram (Google Play)
            "org.telegram.messenger.web",   // Telegram (direct download)
            "org.thunderdog.challegram",    // Telegram X
    };
    private static final long AUTO_OPEN_DELAY_MS = 900;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private Runnable autoOpen;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        ((TextView) findViewById(R.id.version)).setText(getString(R.string.version_label, BuildConfig.VERSION_NAME));
        findViewById(R.id.play).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openGame();
            }
        });
        findViewById(R.id.get_telegram).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openTelegramDownload();
            }
        });

        boolean configured = !BuildConfig.BOT_USERNAME.isEmpty();
        findViewById(R.id.not_configured).setVisibility(configured ? View.GONE : View.VISIBLE);
        findViewById(R.id.play).setEnabled(configured);

        // On a fresh launch, go straight to the game; coming back here later shows the button.
        if (savedInstanceState == null && configured && telegramPackage() != null) {
            autoOpen = new Runnable() {
                @Override
                public void run() {
                    openGame();
                }
            };
            handler.postDelayed(autoOpen, AUTO_OPEN_DELAY_MS);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        boolean hasTelegram = telegramPackage() != null;
        findViewById(R.id.get_telegram).setVisibility(hasTelegram ? View.GONE : View.VISIBLE);
    }

    @Override
    protected void onDestroy() {
        if (autoOpen != null) {
            handler.removeCallbacks(autoOpen);
        }
        super.onDestroy();
    }

    /** The installed Telegram app's package name, or null if none is installed. */
    private String telegramPackage() {
        PackageManager pm = getPackageManager();
        for (String pkg : TELEGRAM_PACKAGES) {
            try {
                pm.getPackageInfo(pkg, 0);
                return pkg;
            } catch (PackageManager.NameNotFoundException ignored) {
                // try the next one
            }
        }
        return null;
    }

    private void openGame() {
        if (BuildConfig.BOT_USERNAME.isEmpty()) {
            return;
        }
        String pkg = telegramPackage();
        if (pkg != null) {
            // Straight into the bot's chat inside the Telegram app.
            Intent intent = new Intent(Intent.ACTION_VIEW,
                    Uri.parse("tg://resolve?domain=" + BuildConfig.BOT_USERNAME));
            intent.setPackage(pkg);
            try {
                startActivity(intent);
                return;
            } catch (ActivityNotFoundException ignored) {
                // fall through to the web link
            }
        }
        // No Telegram app: the t.me link opens it if it appears, or the Telegram website otherwise.
        openUrl("https://t.me/" + BuildConfig.BOT_USERNAME);
    }

    private void openTelegramDownload() {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=org.telegram.messenger")));
        } catch (ActivityNotFoundException e) {
            openUrl("https://telegram.org/android");
        }
    }

    private void openUrl(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (ActivityNotFoundException ignored) {
            // no browser either: nothing more we can do
        }
    }
}
