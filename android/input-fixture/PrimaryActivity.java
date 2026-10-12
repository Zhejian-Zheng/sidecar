package dev.sidecar.inputfixture;
import android.app.Activity;
import android.os.Bundle;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.view.WindowManager;

public class PrimaryActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        TextView label = new TextView(this);
        label.setText(getClass() == PrimaryActivity.class ? "Main input isolation test" : "Secondary input isolation test");
        layout.addView(label);
        EditText input = new EditText(this);
        input.setId(0x7f010001);
        input.setSingleLine(true);
        input.setContentDescription("Isolation test field");
        input.setText(getClass() == PrimaryActivity.class ? "MAIN stays here" : "SECONDARY initial");
        layout.addView(input, new LinearLayout.LayoutParams(-1, 80));
        setContentView(layout);
    }
}
