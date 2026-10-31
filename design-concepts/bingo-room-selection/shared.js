document.querySelectorAll('[data-demo]').forEach((button)=>button.addEventListener('click',()=>{const toast=document.querySelector('.toast');toast.textContent=button.dataset.demo;toast.classList.add('show');clearTimeout(window.roomToast);window.roomToast=setTimeout(()=>toast.classList.remove('show'),1700)}));

