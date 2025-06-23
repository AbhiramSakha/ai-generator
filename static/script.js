document.addEventListener("DOMContentLoaded", function () {
      const items = document.querySelectorAll('.answer-item');
      const prevBtn = document.getElementById('prevBtn');
      const nextBtn = document.getElementById('nextBtn');
      const itemsPerPage = 5;
      let currentPage = 1;
      const totalPages = Math.ceil(items.length / itemsPerPage);

      function showPage(page) {
        const start = (page - 1) * itemsPerPage;
        const end = start + itemsPerPage;

        items.forEach((item, index) => {
          item.style.display = (index >= start && index < end) ? 'list-item' : 'none';
        });

        prevBtn.style.display = page > 1 ? 'inline-block' : 'none';
        nextBtn.style.display = page < totalPages ? 'inline-block' : 'none';
      }

      prevBtn.addEventListener('click', () => {
        if (currentPage > 1) {
          currentPage--;
          showPage(currentPage);
        }
      });

      nextBtn.addEventListener('click', () => {
        if (currentPage < totalPages) {
          currentPage++;
          showPage(currentPage);
        }
      });

      if (items.length > itemsPerPage) {
        showPage(currentPage);
      } else {
        nextBtn.style.display = 'none';
        prevBtn.style.display = 'none';
      }
    });