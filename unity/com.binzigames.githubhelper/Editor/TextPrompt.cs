using System;
using UnityEditor;
using UnityEngine;

namespace BinziGames.GitHubHelper
{
    /// <summary>Small utility dialog with one text field (and an optional checkbox).</summary>
    class TextPrompt : EditorWindow
    {
        string label, value, toggleLabel, okLabel;
        bool toggle, focused;
        Action<string, bool> onOk;

        public static void Show(string title, string label, string initial, string okLabel, Action<string, bool> onOk, string toggleLabel = null, bool toggleInitial = false)
        {
            var w = CreateInstance<TextPrompt>();
            w.titleContent = new GUIContent(title);
            w.label = label; w.value = initial ?? ""; w.okLabel = okLabel; w.onOk = onOk;
            w.toggleLabel = toggleLabel; w.toggle = toggleInitial;
            var h = toggleLabel == null ? 84 : 106;
            w.minSize = w.maxSize = new Vector2(380, h);
            w.ShowUtility();
        }

        void OnGUI()
        {
            GUILayout.Space(8);
            EditorGUILayout.LabelField(label);
            GUI.SetNextControlName("t");
            value = EditorGUILayout.TextField(value);
            if (!focused) { EditorGUI.FocusTextInControl("t"); focused = true; }
            if (toggleLabel != null) toggle = EditorGUILayout.ToggleLeft(toggleLabel, toggle);
            GUILayout.FlexibleSpace();
            using (new GUILayout.HorizontalScope())
            {
                GUILayout.FlexibleSpace();
                if (GUILayout.Button("Cancel", GUILayout.Width(80))) Close();
                GUI.enabled = !string.IsNullOrEmpty(value) && value.Trim().Length > 0;
                bool enter = Event.current.type == EventType.KeyDown && Event.current.keyCode == KeyCode.Return;
                if (GUILayout.Button(okLabel, GUILayout.Width(80)) || (enter && GUI.enabled))
                {
                    var cb = onOk; var v = value.Trim(); var t = toggle;
                    Close();
                    cb(v, t);
                }
                GUI.enabled = true;
            }
            GUILayout.Space(6);
        }
    }
}
