// 左サイドバーの scrollspy：スクロール位置に応じて現在の章リンクをハイライト
(function () {
  function init() {
    var nav = document.querySelector('.side-nav');
    if (!nav) return;
    var links = Array.prototype.slice.call(nav.querySelectorAll('a'));
    var sections = links
      .map(function (a) { return document.getElementById(a.getAttribute('href').slice(1)); })
      .filter(Boolean);
    if (!sections.length) return;

    var ticking = false;
    function update() {
      ticking = false;
      var line = 130; // ビューポート上端からの判定ライン(px)
      var current = sections[0];
      for (var i = 0; i < sections.length; i++) {
        if (sections[i].getBoundingClientRect().top <= line) current = sections[i];
      }
      // 最下部まで来たら最後の章を確実にアクティブ
      if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) {
        current = sections[sections.length - 1];
      }
      var id = current ? current.id : '';
      for (var j = 0; j < links.length; j++) {
        links[j].classList.toggle('active', links[j].getAttribute('href') === '#' + id);
      }
    }
    function onScroll() {
      if (!ticking) { ticking = true; window.requestAnimationFrame(update); }
    }
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    links.forEach(function (a) {
      a.addEventListener('click', function () { setTimeout(update, 60); });
    });
    update();
  }
  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();
