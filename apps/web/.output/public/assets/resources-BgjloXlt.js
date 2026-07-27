import{j as e}from"./main-DJ9jhRmH.js";let h={title:"Resources",description:"Control how much CPU and memory your Docker Compose services can use in LazyCloud."},a={contents:[{heading:"resources",content:"Control how much CPU and memory your services can use."},{heading:"setting-limits",content:"Define resource limits in your compose file:"},{heading:"limits-vs-reservations",content:"Limits — Maximum resources a service can use. If exceeded, the service may be throttled (CPU) or restarted (memory)."},{heading:"limits-vs-reservations",content:"Reservations — Minimum resources guaranteed to the service. LazyCloud ensures these are always available."},{heading:"cpu",content:"CPU is measured in cores:"},{heading:"cpu",content:'"0.5" — Half a CPU core'},{heading:"cpu",content:'"1.0" — One full core'},{heading:"cpu",content:'"2.0" — Two cores'},{heading:"memory",content:"Memory uses standard units:"},{heading:"memory",content:"256M — 256 megabytes"},{heading:"memory",content:"1G — 1 gigabyte"},{heading:"memory",content:"2048M — 2 gigabytes"},{heading:"defaults",content:"If you don't specify resources, LazyCloud applies sensible defaults:"},{heading:"defaults",content:"CPU: 0.25 cores reserved, 2 cores limit"},{heading:"defaults",content:"Memory: 256MB reserved, 2GB limit"},{heading:"defaults",content:`Start with conservative limits and increase based on actual usage. Check the
dashboard to see how much your services actually use.`}],headings:[{id:"resources",content:"Resources"},{id:"setting-limits",content:"Setting Limits"},{id:"limits-vs-reservations",content:"Limits vs Reservations"},{id:"cpu",content:"CPU"},{id:"memory",content:"Memory"},{id:"defaults",content:"Defaults"}]};const c=[{depth:1,url:"#resources",title:e.jsx(e.Fragment,{children:"Resources"})},{depth:2,url:"#setting-limits",title:e.jsx(e.Fragment,{children:"Setting Limits"})},{depth:2,url:"#limits-vs-reservations",title:e.jsx(e.Fragment,{children:"Limits vs Reservations"})},{depth:2,url:"#cpu",title:e.jsx(e.Fragment,{children:"CPU"})},{depth:2,url:"#memory",title:e.jsx(e.Fragment,{children:"Memory"})},{depth:2,url:"#defaults",title:e.jsx(e.Fragment,{children:"Defaults"})}];function r(i){const s={code:"code",h1:"h1",h2:"h2",li:"li",p:"p",pre:"pre",span:"span",strong:"strong",ul:"ul",...i.components},{Tip:n}=s;return n||t("Tip"),e.jsxs(e.Fragment,{children:[e.jsx(s.h1,{id:"resources",children:"Resources"}),`
`,e.jsx(s.p,{children:"Control how much CPU and memory your services can use."}),`
`,e.jsx(s.h2,{id:"setting-limits",children:"Setting Limits"}),`
`,e.jsx(s.p,{children:"Define resource limits in your compose file:"}),`
`,e.jsx(e.Fragment,{children:e.jsx(s.pre,{className:"shiki shiki-themes github-light github-dark",style:{"--shiki-light":"#24292e","--shiki-dark":"#e1e4e8","--shiki-light-bg":"#fff","--shiki-dark-bg":"#24292e"},tabIndex:"0",icon:'<svg viewBox="0 0 24 24"><path d="M 6,1 C 4.354992,1 3,2.354992 3,4 v 16 c 0,1.645008 1.354992,3 3,3 h 12 c 1.645008,0 3,-1.354992 3,-3 V 8 7 A 1.0001,1.0001 0 0 0 20.707031,6.2929687 l -5,-5 A 1.0001,1.0001 0 0 0 15,1 h -1 z m 0,2 h 7 v 3 c 0,1.645008 1.354992,3 3,3 h 3 v 11 c 0,0.564129 -0.435871,1 -1,1 H 6 C 5.4358712,21 5,20.564129 5,20 V 4 C 5,3.4358712 5.4358712,3 6,3 Z M 15,3.4140625 18.585937,7 H 16 C 15.435871,7 15,6.5641288 15,6 Z" fill="currentColor" /></svg>',children:e.jsxs(s.code,{children:[e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"services"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:":"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"  api"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:":"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"    build"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:": "}),e.jsx(s.span,{style:{"--shiki-light":"#005CC5","--shiki-dark":"#79B8FF"},children:"."})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"    deploy"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:":"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"      resources"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:":"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"        limits"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:":"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"          cpus"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:": "}),e.jsx(s.span,{style:{"--shiki-light":"#032F62","--shiki-dark":"#9ECBFF"},children:"'2.0'"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"          memory"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:": "}),e.jsx(s.span,{style:{"--shiki-light":"#032F62","--shiki-dark":"#9ECBFF"},children:"1024M"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"        reservations"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:":"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"          cpus"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:": "}),e.jsx(s.span,{style:{"--shiki-light":"#032F62","--shiki-dark":"#9ECBFF"},children:"'0.5'"})]}),`
`,e.jsxs(s.span,{className:"line",children:[e.jsx(s.span,{style:{"--shiki-light":"#22863A","--shiki-dark":"#85E89D"},children:"          memory"}),e.jsx(s.span,{style:{"--shiki-light":"#24292E","--shiki-dark":"#E1E4E8"},children:": "}),e.jsx(s.span,{style:{"--shiki-light":"#032F62","--shiki-dark":"#9ECBFF"},children:"512M"})]})]})})}),`
`,e.jsx(s.h2,{id:"limits-vs-reservations",children:"Limits vs Reservations"}),`
`,e.jsxs(s.ul,{children:[`
`,e.jsxs(s.li,{children:[e.jsx(s.strong,{children:"Limits"})," — Maximum resources a service can use. If exceeded, the service may be throttled (CPU) or restarted (memory)."]}),`
`,e.jsxs(s.li,{children:[e.jsx(s.strong,{children:"Reservations"})," — Minimum resources guaranteed to the service. LazyCloud ensures these are always available."]}),`
`]}),`
`,e.jsx(s.h2,{id:"cpu",children:"CPU"}),`
`,e.jsx(s.p,{children:"CPU is measured in cores:"}),`
`,e.jsxs(s.ul,{children:[`
`,e.jsxs(s.li,{children:[e.jsx(s.code,{children:'"0.5"'})," — Half a CPU core"]}),`
`,e.jsxs(s.li,{children:[e.jsx(s.code,{children:'"1.0"'})," — One full core"]}),`
`,e.jsxs(s.li,{children:[e.jsx(s.code,{children:'"2.0"'})," — Two cores"]}),`
`]}),`
`,e.jsx(s.h2,{id:"memory",children:"Memory"}),`
`,e.jsx(s.p,{children:"Memory uses standard units:"}),`
`,e.jsxs(s.ul,{children:[`
`,e.jsxs(s.li,{children:[e.jsx(s.code,{children:"256M"})," — 256 megabytes"]}),`
`,e.jsxs(s.li,{children:[e.jsx(s.code,{children:"1G"})," — 1 gigabyte"]}),`
`,e.jsxs(s.li,{children:[e.jsx(s.code,{children:"2048M"})," — 2 gigabytes"]}),`
`]}),`
`,e.jsx(s.h2,{id:"defaults",children:"Defaults"}),`
`,e.jsx(s.p,{children:"If you don't specify resources, LazyCloud applies sensible defaults:"}),`
`,e.jsxs(s.ul,{children:[`
`,e.jsx(s.li,{children:"CPU: 0.25 cores reserved, 2 cores limit"}),`
`,e.jsx(s.li,{children:"Memory: 256MB reserved, 2GB limit"}),`
`]}),`
`,e.jsx(n,{children:e.jsx(s.p,{children:`Start with conservative limits and increase based on actual usage. Check the
dashboard to see how much your services actually use.`})})]})}function d(i={}){const{wrapper:s}=i.components||{};return s?e.jsx(s,{...i,children:e.jsx(r,{...i})}):r(i)}function t(i,s){throw new Error("Expected component `"+i+"` to be defined: you likely forgot to import, pass, or provide it.")}export{d as default,h as frontmatter,a as structuredData,c as toc};
