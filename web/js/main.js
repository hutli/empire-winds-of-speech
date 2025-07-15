const INTERSECTION_SILENCE = 500;

let audio_playing = null;
let is_playing = false;
let playback_rate = 1.0;
let start_delay = 0;
let TIMEOUTS = [];
let CURRENT_AUDIOS = [];

function my_highlight(span, length) {
  if (is_playing) {
    let elem = document.getElementById(span);
    elem.classList.add("active_span");
    elem.scrollIntoView({
      behavior: "smooth",
      block: "center",
      inline: "center",
    });

    setTimeout(() => {
      document.getElementById(span).classList.remove("active_span");
    }, length);
  }
}

function start_highlights(audios) {
  if (audios.length) {
    for (let i = 0; i < audios[0][0].length; i++) {
      let timeout =
        (audios[0][1][i].start - audio_playing.currentTime * 1000) /
        playback_rate;
      if (timeout >= 0) {
        TIMEOUTS.push(
          setTimeout(() => {
            my_highlight(
              audios[0][0][i],
              audios[0][1][i].length / playback_rate,
            );
          }, timeout),
        );
      }
    }
  }
}

function clear_highlights() {
  for (let h of TIMEOUTS) {
    clearTimeout(h);
  }
  TIMEOUTS = [];
}

async function my_play(audios, outro) {
  CURRENT_AUDIOS = audios;
  if (audios.length > 0) {
    audios[0][2].addEventListener("ended", () => {
      setTimeout(() => my_play(audios.slice(1), outro), INTERSECTION_SILENCE);
    });
    audio_playing = audios[0][2];
  } else {
    audio_playing = outro;
    audio_playing.addEventListener("ended", () => {
      pausePlayback();
      audio_playing = null;
    });
  }

  audio_playing.playbackRate = playback_rate;
  if (is_playing) {
    audio_playing.play();
    start_highlights(audios);
  } else {
    audio_playing.pause();
    clear_highlights(audios);
  }
}

function changeFontSize(e) {
  document.querySelector("body").style.fontSize = e.value;
}

function changeFont(e) {
  if (e.value == "Arial") {
    document.querySelector("body").classList.add("arial");
  } else if (e.value == "Open Dyslexic") {
    document.querySelector("body").classList.remove("arial");
  } else {
    console.error(`Unknown font ${e.value}`);
  }
}

function changeLineHeight(e) {
  document
    .querySelectorAll("p")
    .forEach((ee) => (ee.style.lineHeight = e.value));
}

function createLinkButton(href, button_text) {
  let a = document.createElement("a");
  a.href = href;
  let btn = document.createElement("button");
  btn.innerText = button_text;
  a.appendChild(btn);
  return a;
}

function createRoundButton(innerHTML) {
  let btn_inner = document.createElement("b");
  btn_inner.innerHTML = innerHTML;
  btn_inner.classList.add("fas");
  let btn = document.createElement("div");
  btn.classList.add("audio-button");
  btn.appendChild(btn_inner);
  return btn;
}

function createRoundLinkButton(href, innerHTML) {
  let a = document.createElement("a");
  a.style.textDecoration = "none";
  a.style.margin = "auto";
  a.href = href;
  a.appendChild(createRoundButton(innerHTML));
  return a;
}

function changePlaybackSpeed() {
  if (audio_playing) {
    slider = document.querySelector("#playback-speed-slider");
    playback_rate = slider.value / 100;
    audio_playing.playbackRate = playback_rate;
    clear_highlights(CURRENT_AUDIOS);
    if (is_playing) {
      start_highlights(CURRENT_AUDIOS);
    }
  }
}

function resumePlayback() {
  if (audio_playing) {
    is_playing = true;
    document.querySelector("#resume-btn").classList.add("audio-button-active");
    document
      .querySelector("#pause-btn")
      .classList.remove("audio-button-active");
    audio_playing.play();
    start_highlights(CURRENT_AUDIOS);
  }
}

function pausePlayback() {
  if (audio_playing) {
    is_playing = false;
    document
      .querySelector("#resume-btn")
      .classList.remove("audio-button-active");
    document.querySelector("#pause-btn").classList.add("audio-button-active");
    audio_playing.pause();
    clear_highlights(CURRENT_AUDIOS);
  }
}

async function populateManuscriptContent(manuscript) {
  let article_content = document.querySelector("#article-content");
  article_content.innerHTML = "";
  let progress = document.createElement("p");

  if (manuscript.state == "generating") {
    if (manuscript.progress == 0) {
      progress.innerText = `Waiting - Article still in queue...`;
      article_content.appendChild(progress);
    } else {
      progress.innerText = `Generating article - ${(
        manuscript.progress * 100
      ).toFixed(2)}%`;
      article_content.appendChild(progress);
    }
  }

  let audios = [];
  let i = 0;

  for (const section of manuscript.sections) {
    let section_elem = document.createElement(section.section_type);
    if (section.section_type == "img") {
      section_elem.src = section.src;
      section_elem.alt = section.alt;
    } else {
      section_elem.classList.add("section");
      section_elem.id = i;
      section_elem.title = "Start audio from this section";
      section_elem.onclick = (e) => {
        if (audio_playing) {
          audio_playing.pause();
          audio_playing.currentTime = 0;
        }

        clear_highlights();
        my_play(
          audios.slice(Number(e.srcElement.id.split("_")[0])),
          new Audio(manuscript.outro.audio_url),
        );
      };

      let span_ids = [];
      for (let [ii, span] of section.spans.entries()) {
        let span_elem = document.createElement(
          section.section_type != "ul" && section.section_type != "ol"
            ? "span"
            : "li",
        );

        let span_id = `${String(i).padStart(4, "0")}_${String(ii).padStart(
          4,
          "0",
        )}`;
        span_ids.push(span_id);
        span_elem.textContent = span.text;
        span_elem.id = span_id;
        section_elem.appendChild(span_elem);
      }
      if (section.alignment_url && section.audio_url) {
        audios.push([
          span_ids,
          await (await fetch(section.alignment_url)).json(),
          new Audio(section.audio_url),
        ]);
      }
      section_elem.innerHTML = section_elem.innerHTML.replaceAll(
        "</span><",
        "</span> <",
      );
      i++;
    }
    article_content.appendChild(section_elem);
  }

  console.debug(manuscript.url);
  if (manuscript.url) {
    article_content.appendChild(document.createElement("hr"));
    let a = document.createElement("a");
    a.href = manuscript.url;
    a.target = "_blank";
    a.innerText = manuscript.url;
    article_content.appendChild(a);
  }

  if (manuscript.state != "generating") {
    document
      .querySelector("#resume-btn")
      .classList.remove("audio-button-active");
    document.querySelector("#pause-btn").classList.add("audio-button-active");
  }
  is_playing = false;
  if (manuscript.outro && manuscript.outro.audio_url) {
    my_play(audios, new Audio(manuscript.outro.audio_url));
  }
}

function alphabeticallyRankName(a, b) {
  if (a.name < b.name) {
    return -1;
  }
  if (a.name > b.name) {
    return 1;
  }
  return 0;
}

function updateMeta(manuscript) {
  if (manuscript.title) {
    document.title = manuscript.title;
    document
      .querySelector('meta[property="og:title"]')
      .setAttribute("content", manuscript.title);
    document
      .querySelector('meta[property="twitter:title"]')
      .setAttribute("content", manuscript.title);
  }
  for (const section of manuscript.sections) {
    if (section.section_type == "p") {
      let overview = section.spans.map((s) => s.text).join(" ");
      if (overview) {
        document
          .querySelector('meta[name="description"]')
          .setAttribute("content", overview);
        document
          .querySelector('meta[property="og:description"]')
          .setAttribute("content", overview);
        document
          .querySelector('meta[property="twitter:description"]')
          .setAttribute("content", overview);
      }
      return;
    }
  }
  return null;
}

// MAIN
let params = new URLSearchParams(document.location.search);
let p_name = params.get("name");
let p_scraping_url = params.get("scraping_url");
let p_path = params.get("path");

if (!p_name) {
  p_name = location.pathname.split("/").slice(-1)[0];
}

if (!p_path || !p_name) {
  p_path = "/api/manuscript";
}
let url =
  `${p_path}/${p_name}` +
  (p_scraping_url ? `?scraping_url=${p_scraping_url}` : "");

console.log(p_scraping_url);
console.log(p_name);
console.log(p_path);
console.log(url);

fetch(url).then((response) => {
  if (response.status == 200) {
    response.json().then((manuscript) => {
      updateMeta(manuscript);
      if (manuscript.complete_audio_url) {
        let download_btn = document.getElementById("download-btn");
        download_btn.href = manuscript.complete_audio_url;
        download_btn.download = `${manuscript["_id"]}.mp3`;
        download_btn.classList.remove("download-button-hidden");
      }
      populateManuscriptContent(manuscript);
    });
  } else {
    console.error(response);
  }
});
