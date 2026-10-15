package dev.sidecar.textbridge;

import android.graphics.Rect;
import android.view.accessibility.AccessibilityNodeInfo;
import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;

/** In-memory Android node regression tests; does not connect to or modify any app. */
public final class VerificationTest {
    private static AccessibilityNodeInfo node(String id, String text) {
        AccessibilityNodeInfo node = AccessibilityNodeInfo.obtain();
        node.setPackageName("com.test");
        node.setClassName("android.widget.EditText");
        node.setViewIdResourceName(id);
        node.setContentDescription("field");
        node.setBoundsInScreen(new Rect(0, 0, 100, 100));
        node.setEditable(true);
        node.setEnabled(true);
        node.setVisibleToUser(true);
        node.addAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_TEXT);
        node.setText(text);
        return node;
    }

    public static void main(String[] args) throws Exception {
        Method verify;
        try {
            verify = Main.class.getDeclaredMethod("confirmsNodes", List.class,
                    JSONObject.class, String.class, String.class);
        } catch (NoSuchMethodException failure) {
            throw new AssertionError("Missing target-specific post-write verification", failure);
        }
        verify.setAccessible(true);
        JSONObject selector = new JSONObject().put("resource_id", "body")
                .put("class_name", "android.widget.EditText").put("description", "field")
                .put("old_text", "before").put("bounds", new JSONArray("[0,0,100,100]"));
        AccessibilityNodeInfo target = node("body", "before");
        AccessibilityNodeInfo other = node("subject", "Hello");
        check(verify, selector, Arrays.asList(target, other), false, "another field cannot verify write");
        target.setText("Hello");
        check(verify, selector, Arrays.asList(target, other), true, "fresh target verifies write");
        check(verify, selector, Arrays.asList(target, node("body", "before")), false, "ambiguous identity");
        target.setVisibleToUser(false);
        check(verify, selector, Arrays.asList(target, other), false, "hidden target");
        target.setVisibleToUser(true);
        target.setPassword(true);
        check(verify, selector, Arrays.asList(target, other), false, "password target");
        target.setPassword(false);
        target.setBoundsInScreen(new Rect(20, 20, 120, 120));
        check(verify, selector, Arrays.asList(target, other), false, "moved target is uncertain");
        Method written = Main.class.getDeclaredMethod("writtenContent", AccessibilityNodeInfo.class,
                String.class, String.class);
        written.setAccessible(true);
        if (!(Boolean) written.invoke(null, target, "com.test", "Hello"))
            throw new AssertionError("original resized node must verify");
        target.setText("Hello  world");
        if ((Boolean) written.invoke(null, target, "com.test", "Hello world"))
            throw new AssertionError("whitespace changes must not verify exact body");
        System.out.println("6 bridge verification checks passed; resized-node checks passed");
    }

    private static void check(Method verify, JSONObject selector, List<AccessibilityNodeInfo> nodes,
                              boolean expected, String scenario) throws Exception {
        boolean actual = (Boolean) verify.invoke(null, nodes, selector, "com.test", "Hello");
        if (actual != expected) throw new AssertionError(scenario);
    }
}
