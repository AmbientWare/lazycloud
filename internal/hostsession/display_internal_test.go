package hostsession

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// A result's HTML keeps what it shows and loses everything that could
// redirect the dashboard's frame, load from the network or run code.
func TestResultHTMLCannotNavigateOrLoad(t *testing.T) {
	cases := []struct{ in, want string }{
		{`<table class="df"><tr><th>a</th></tr><tr><td>1 &lt; 2</td></tr></table>`, `<table class="df"><tr><th>a</th></tr><tr><td>1 &lt; 2</td></tr></table>`},
		{`<meta http-equiv="refresh" content="0;url=https://evil.example/"><p>x</p>`, `<p>x</p>`},
		{`<base href="https://evil.example/"><a href="/login">sign in</a>`, `<a>sign in</a>`},
		{`<link rel="stylesheet" href="https://evil.example/a.css"><b>bold</b>`, `<b>bold</b>`},
		{`<script>top.location="https://evil.example"</script><i>i</i>`, `<i>i</i>`},
		{`<iframe src="https://evil.example"><p>inside</p></iframe><u>u</u>`, `<u>u</u>`},
		{`<form action="https://evil.example"><input name="q"></form>ok`, `ok`},
		{`<img src="https://evil.example/pixel.png"><img src="data:image/png;base64,AA==" onerror="alert(1)">`, `<img><img src="data:image/png;base64,AA==">`},
		{`<svg><a xlink:href="https://evil.example"><set attributeName="href" to="https://evil.example"/><text>t</text></a></svg>`, `<svg><a><text>t</text></a></svg>`},
		{`<style>td > b { color: red }</style><div onclick="x()">d</div>`, `<style>td > b { color: red }</style><div>d</div>`},
	}
	for _, c := range cases {
		if got := inertHTML(c.in); got != c.want {
			t.Errorf("inertHTML(%q)\n got %q\nwant %q", c.in, got, c.want)
		}
	}
}

// The host session keeps the inert HTML, and drops a display with nothing
// left to show.
func TestResultDisplayKeepsOnlyInertHTML(t *testing.T) {
	kept, err := resultDisplay([]byte(`{"text": "t", "rich": {"kind": "html", "html": "<meta http-equiv=\"refresh\" content=\"0;url=https://evil.example\"><table></table>"}}`))
	if err != nil {
		t.Fatal(err)
	}
	var d apitypes.ResultDisplay
	if err := json.Unmarshal(kept, &d); err != nil || d.Rich == nil || d.Rich.Html == nil || strings.Contains(*d.Rich.Html, "meta") {
		t.Fatalf("kept %s %v", kept, err)
	}
	if _, err := resultDisplay([]byte(`{"text": "t", "rich": {"kind": "html", "html": "<script>x</script>"}}`)); err == nil {
		t.Fatal("a display whose HTML is only a script is dropped")
	}
}
